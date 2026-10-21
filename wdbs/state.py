"""WDBS operational state store (SQLite, owned by WDBS, separate from FS42).

Holds what FS42 does not: actual playout history, the live-state snapshot,
operator overrides, replan runs, and the event log.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_MIGRATIONS = {
    1: """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

    -- One row per status-socket write (a plan segment starting to play).
    CREATE TABLE segments (
        id INTEGER PRIMARY KEY,
        observed_at TEXT NOT NULL,
        status_ts TEXT,
        status TEXT,
        channel INTEGER,
        network TEXT,
        file_path TEXT,
        content_type TEXT,
        offset_s REAL,
        segment_s REAL,
        title TEXT,
        UNIQUE (status_ts, channel, file_path, offset_s)
    );

    -- One row per feature episode actually put on air.
    CREATE TABLE airings (
        id INTEGER PRIMARY KEY,
        channel INTEGER NOT NULL,
        network TEXT,
        file_path TEXT NOT NULL,
        catalog_id INTEGER,
        tag TEXT,
        title TEXT,
        season INTEGER,
        episode INTEGER,
        block_id INTEGER,
        block_start TEXT,
        block_end TEXT,
        schedule_match INTEGER,
        started_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        ended_at TEXT,
        feature_duration_s REAL,
        position_s REAL DEFAULT 0,
        completed INTEGER NOT NULL DEFAULT 0,
        end_reason TEXT
    );
    CREATE INDEX idx_airings_tag_time ON airings (channel, tag, started_at);
    CREATE INDEX idx_airings_time ON airings (started_at);
    CREATE INDEX idx_airings_open ON airings (ended_at);

    CREATE TABLE live_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    );

    -- Local operator overrides layered over the Notion-derived manifest.
    CREATE TABLE program_overrides (
        channel INTEGER NOT NULL,
        tag TEXT NOT NULL,
        active INTEGER,
        manual_boost REAL,
        note TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (channel, tag)
    );

    -- A locked slot keeps its program; the planner may not change it.
    CREATE TABLE slot_locks (
        channel INTEGER NOT NULL,
        slot_start TEXT NOT NULL,
        tag TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (channel, slot_start)
    );

    CREATE TABLE replan_runs (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        channel INTEGER NOT NULL,
        requested_cutoff TEXT,
        effective_cutoff TEXT,
        mode TEXT NOT NULL,
        lineup_source TEXT,
        backup_path TEXT,
        blocks_deleted INTEGER,
        blocks_inserted INTEGER,
        ok INTEGER NOT NULL,
        report_json TEXT
    );

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        channel INTEGER,
        payload TEXT
    );
    CREATE INDEX idx_events_kind_ts ON events (kind, ts);
    """,
    2: """
    ALTER TABLE airings ADD COLUMN join_offset_s REAL DEFAULT 0;
    """,
}


def now_str(when: dt.datetime | None = None) -> str:
    return (when or dt.datetime.now()).replace(microsecond=0).isoformat(sep=" ")


class StateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- schema -----------------------------------------------------------

    def schema_version(self) -> int:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    def migrate(self):
        current = self.schema_version()
        for version in sorted(v for v in _MIGRATIONS if v > current):
            self.conn.executescript("BEGIN;" + _MIGRATIONS[version] + "COMMIT;")
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(version),))

    # -- live state -------------------------------------------------------

    def set_live(self, values: dict, when: dt.datetime | None = None):
        ts = now_str(when)
        with self.conn:
            for k, v in values.items():
                self.conn.execute(
                    "INSERT OR REPLACE INTO live_state (key, value, updated_at) VALUES (?, ?, ?)",
                    (k, json.dumps(v, default=str), ts))

    def get_live(self) -> dict:
        out = {}
        for r in self.conn.execute("SELECT key, value, updated_at FROM live_state"):
            out[r["key"]] = {"value": json.loads(r["value"]), "updated_at": r["updated_at"]}
        return out

    # -- events -----------------------------------------------------------

    def emit(self, kind: str, payload: dict | None = None, channel: int | None = None,
             when: dt.datetime | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (ts, kind, channel, payload) VALUES (?, ?, ?, ?)",
                (now_str(when), kind, channel, json.dumps(payload or {}, default=str)))
        return cur.lastrowid

    def events(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM events"
        args: list = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) | {"payload": json.loads(r["payload"] or "{}")}
                for r in self.conn.execute(sql, args)]

    # -- overrides --------------------------------------------------------

    def set_override(self, channel: int, tag: str, *, active: bool | None = None,
                     manual_boost: float | None = None, note: str | None = None):
        existing = self.get_overrides(channel).get(tag, {})
        active_v = existing.get("active") if active is None else int(active)
        boost_v = existing.get("manual_boost") if manual_boost is None else manual_boost
        note_v = existing.get("note") if note is None else note
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO program_overrides (channel, tag, active, manual_boost, note, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (channel, tag, active_v, boost_v, note_v, now_str()))

    def clear_override(self, channel: int, tag: str):
        with self.conn:
            self.conn.execute("DELETE FROM program_overrides WHERE channel = ? AND tag = ?", (channel, tag))

    def get_overrides(self, channel: int) -> dict[str, dict]:
        return {r["tag"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM program_overrides WHERE channel = ?", (channel,))}

    # -- slot locks -------------------------------------------------------

    def lock_slot(self, channel: int, slot_start: dt.datetime, tag: str, note: str | None = None):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO slot_locks (channel, slot_start, tag, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (channel, now_str(slot_start), tag, note, now_str()))

    def unlock_slot(self, channel: int, slot_start: dt.datetime):
        with self.conn:
            self.conn.execute("DELETE FROM slot_locks WHERE channel = ? AND slot_start = ?",
                              (channel, now_str(slot_start)))

    def slot_locks(self, channel: int, start: dt.datetime | None = None,
                   end: dt.datetime | None = None) -> dict[str, str]:
        sql = "SELECT slot_start, tag FROM slot_locks WHERE channel = ?"
        args: list = [channel]
        if start:
            sql += " AND slot_start >= ?"
            args.append(now_str(start))
        if end:
            sql += " AND slot_start < ?"
            args.append(now_str(end))
        return {r["slot_start"]: r["tag"] for r in self.conn.execute(sql, args)}

    # -- replan runs ------------------------------------------------------

    def record_replan(self, *, channel: int, requested_cutoff, effective_cutoff, mode: str,
                      lineup_source: str | None, backup_path: str | None, blocks_deleted: int,
                      blocks_inserted: int, ok: bool, report: dict) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO replan_runs (created_at, channel, requested_cutoff, effective_cutoff, mode, "
                "lineup_source, backup_path, blocks_deleted, blocks_inserted, ok, report_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now_str(), channel, str(requested_cutoff) if requested_cutoff else None,
                 str(effective_cutoff) if effective_cutoff else None, mode, lineup_source,
                 backup_path, blocks_deleted, blocks_inserted, int(ok), json.dumps(report, default=str)))
        return cur.lastrowid
