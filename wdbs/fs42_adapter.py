"""Read-only view of FieldStation42.

This is the only module (besides wdbs.replan's executor) that knows the FS42
SQLite schema, station config layout, and status-socket format. Everything here
opens the FS42 database read-only; nothing in this module writes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import Settings
from .episodes import parse_episode

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_ts(value) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    s = str(value).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", TS_FORMAT, "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized timestamp: {value!r}")


@contextmanager
def connect_ro(db_path: Path, timeout: float = 30.0):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Stations
# --------------------------------------------------------------------------

@dataclass
class Station:
    channel: int
    network_name: str
    network_type: str
    config_path: Path
    raw: dict

    @property
    def content_dir(self) -> str | None:
        return self.raw.get("content_dir")

    def day_template(self, weekday_name: str) -> dict | None:
        day = self.raw.get(weekday_name)
        if isinstance(day, str):
            return (self.raw.get("day_templates") or {}).get(day)
        return day if isinstance(day, dict) else None


def load_stations(settings: Settings) -> list[Station]:
    stations = []
    for path in sorted(settings.confs_dir.glob("*.json")):
        try:
            obj = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        conf = obj.get("station_conf") if isinstance(obj, dict) else None
        if not isinstance(conf, dict) or "channel_number" not in conf:
            continue
        stations.append(Station(
            channel=int(conf["channel_number"]),
            network_name=conf.get("network_name", ""),
            network_type=conf.get("network_type", "standard"),
            config_path=path,
            raw=conf,
        ))
    return sorted(stations, key=lambda s: s.channel)


def station_for_channel(settings: Settings, channel: int) -> Station:
    for s in load_stations(settings):
        if s.channel == channel:
            return s
    raise LookupError(f"No FieldStation42 station configured for channel {channel}")


def station_for_name(settings: Settings, name: str) -> Station | None:
    for s in load_stations(settings):
        if s.network_name == name:
            return s
    return None


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

@dataclass
class CatalogEntry:
    id: int
    station: str
    path: str
    realpath: str | None
    title: str
    duration: float
    tag: str
    count: int
    content_type: str

    @property
    def episode(self):
        return parse_episode(self.path)


def get_catalog(conn, station: str) -> dict[int, CatalogEntry]:
    rows = conn.execute(
        "SELECT id, station, path, realpath, title, duration, tag, count, content_type "
        "FROM catalog_entries WHERE station = ?", (station,)).fetchall()
    return {r[0]: CatalogEntry(*r) for r in rows}


def catalog_by_path(catalog: dict[int, CatalogEntry]) -> dict[str, CatalogEntry]:
    out = {}
    for e in catalog.values():
        out.setdefault(e.path, e)
        if e.realpath:
            out.setdefault(e.realpath, e)
    return out


# --------------------------------------------------------------------------
# Schedule blocks
# --------------------------------------------------------------------------

@dataclass
class Block:
    id: int
    station: str
    liquid_type: str
    start: dt.datetime
    end: dt.datetime
    title: str
    sequence_key: dict | None
    content_ids: list[int]
    plan_json: str = field(repr=False, default="")

    @property
    def sequence_name(self) -> str | None:
        return (self.sequence_key or {}).get("sequence_name")

    @property
    def tag_path(self) -> str | None:
        return (self.sequence_key or {}).get("tag_path")

    def contains(self, when: dt.datetime) -> bool:
        return self.start <= when < self.end

    def fingerprint(self) -> str:
        raw = json.dumps([self.id, self.liquid_type, self.start.isoformat(), self.end.isoformat(),
                          self.title, self.sequence_key, self.content_ids, self.plan_json])
        return hashlib.sha256(raw.encode()).hexdigest()

    def plan_paths(self) -> list[dict]:
        try:
            return json.loads(self.plan_json) if self.plan_json else []
        except ValueError:
            return []


def _parse_content_ids(raw) -> list[int]:
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    values = value if isinstance(value, list) else [value]
    out = []
    for v in values:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


def get_blocks(conn, station: str, start: dt.datetime | None = None,
               end: dt.datetime | None = None) -> list[Block]:
    sql = ("SELECT id, station, liquid_type, start_time, end_time, title, sequence_key, "
           "content_json, plan_json FROM liquid_blocks WHERE station = ?")
    args: list = [station]
    if end is not None:
        sql += " AND start_time < ?"
        args.append(end.strftime(TS_FORMAT))
    if start is not None:
        sql += " AND end_time > ?"
        args.append(start.strftime(TS_FORMAT))
    sql += " ORDER BY start_time, id"
    blocks = []
    for r in conn.execute(sql, args):
        seq = None
        if r[6]:
            try:
                seq = json.loads(r[6])
            except ValueError:
                seq = None
        blocks.append(Block(r[0], r[1], r[2], parse_ts(r[3]), parse_ts(r[4]), r[5], seq,
                            _parse_content_ids(r[7]), r[8] or ""))
    return blocks


def block_at(blocks: list[Block], when: dt.datetime) -> Block | None:
    for b in blocks:
        if b.contains(when):
            return b
    return None


def schedule_extent(conn, station: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    row = conn.execute("SELECT min(start_time), max(end_time) FROM liquid_blocks WHERE station = ?",
                       (station,)).fetchone()
    return parse_ts(row[0]), parse_ts(row[1])


# --------------------------------------------------------------------------
# Sequences
# --------------------------------------------------------------------------

@dataclass
class Sequence:
    id: int
    station: str
    sequence_name: str
    tag_path: str
    start_perc: float
    end_perc: float
    current_index: int
    parent_tag: str | None
    episodes: list[str]

    # FS42's NamedSequence sorts episodes by fpath and indexes current_index into
    # that sorted list; episodes here are stored already sorted the same way.
    @property
    def end_index(self) -> int:
        return min(math.floor(self.end_perc * len(self.episodes)), len(self.episodes))

    @property
    def start_index(self) -> int:
        if self.start_perc < 0:
            return 0
        return min(math.floor(self.start_perc * len(self.episodes)), max(self.end_index - 1, 0))

    def index_of(self, fpath: str) -> int | None:
        try:
            return self.episodes.index(fpath)
        except ValueError:
            return None

    def next_path(self) -> str | None:
        """The episode FS42 would pick next (mirrors SequenceAPI wrap logic)."""
        if not self.episodes:
            return None
        idx = max(self.current_index, -1)
        if idx > len(self.episodes):
            idx = 0
        if idx >= self.end_index:
            idx = self.start_index
        if idx >= len(self.episodes):
            return None
        return self.episodes[idx]

    def follows(self, idx: int) -> bool:
        """True when the cursor resumes right after episode idx. A cursor past the end
        and one already wrapped to the start pick the same next episode."""
        return self.current_index == idx + 1 or self.next_path() == replace(self, current_index=idx + 1).next_path()


def get_sequences(conn, station: str) -> dict[tuple[str, str], Sequence]:
    rows = conn.execute(
        "SELECT id, station, sequence_name, tag_path, start_perc, end_perc, current_index, parent_tag "
        "FROM named_sequence WHERE station = ?", (station,)).fetchall()
    out = {}
    for r in rows:
        eps = [x[0] for x in conn.execute(
            "SELECT fpath FROM sequence_entries WHERE named_sequence_id = ? ORDER BY sequence_index",
            (r[0],))]
        out[(r[2], r[3])] = Sequence(*r, episodes=sorted(eps))
    return out


def sequence_group_state_count(conn, station: str) -> int:
    return conn.execute("SELECT count(*) FROM sequence_group_state WHERE station = ?",
                        (station,)).fetchone()[0]


# --------------------------------------------------------------------------
# Fingerprints (for invariant verification)
# --------------------------------------------------------------------------

def catalog_fingerprint(conn, station: str) -> dict:
    rows = conn.execute(
        "SELECT id, path, realpath, title, duration, tag, content_type FROM catalog_entries "
        "WHERE station = ? ORDER BY id", (station,)).fetchall()
    h = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    return {"rows": len(rows), "ids": [r[0] for r in rows], "hash": h}


def sequences_fingerprint(conn, station: str) -> dict:
    ns = conn.execute(
        "SELECT id, sequence_name, tag_path, start_perc, end_perc, parent_tag FROM named_sequence "
        "WHERE station = ? ORDER BY id", (station,)).fetchall()
    se = conn.execute(
        "SELECT se.named_sequence_id, se.sequence_index, se.fpath FROM sequence_entries se "
        "JOIN named_sequence n ON n.id = se.named_sequence_id WHERE n.station = ? "
        "ORDER BY se.named_sequence_id, se.sequence_index", (station,)).fetchall()
    return {
        "sequences": len(ns),
        "entries": len(se),
        "hash": hashlib.sha256(json.dumps([ns, se]).encode()).hexdigest(),
    }


def sequence_cursors(conn, station: str) -> dict[str, int]:
    return {f"{r[0]}|{r[1]}": r[2] for r in conn.execute(
        "SELECT sequence_name, tag_path, current_index FROM named_sequence WHERE station = ?",
        (station,))}


def other_tables_fingerprint(conn) -> dict:
    out = {}
    for table in ("file_meta", "break_points", "chapter_points", "sequence_group_state"):
        try:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        except sqlite3.Error:
            continue
        out[table] = hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()
    return out


# --------------------------------------------------------------------------
# Live player
# --------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})$")


def _hms(value: str) -> float | None:
    m = _DURATION_RE.match(value.strip())
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return float(h * 3600 + mi * 60 + s)


@dataclass
class PlayerStatus:
    status: str
    network_name: str
    channel: int | None
    timestamp: dt.datetime | None
    title: str | None
    file_path: str | None
    content_type: str | None
    offset_s: float | None
    segment_s: float | None
    raw: dict

    @property
    def key(self) -> tuple:
        return (self.timestamp.isoformat() if self.timestamp else None, self.channel,
                self.file_path, self.offset_s)


def parse_status(raw: dict) -> PlayerStatus:
    offset = segment = None
    duration = raw.get("duration")
    if isinstance(duration, str) and "/" in duration:
        a, b = duration.split("/", 1)
        offset, segment = _hms(a), _hms(b)
    ts = None
    if raw.get("timestamp"):
        try:
            ts = parse_ts(raw["timestamp"])
        except ValueError:
            ts = None
    ch = raw.get("channel_number")
    try:
        ch = int(ch) if ch is not None else None
    except (TypeError, ValueError):
        ch = None
    if ch is not None and ch < 0:
        ch = None
    return PlayerStatus(
        status=raw.get("status", "unknown"),
        network_name=raw.get("network_name", ""),
        channel=ch,
        timestamp=ts,
        title=raw.get("title"),
        file_path=raw.get("file_path"),
        content_type=raw.get("content_type"),
        offset_s=offset,
        segment_s=segment,
        raw=raw,
    )


def read_status(settings: Settings) -> PlayerStatus | None:
    try:
        text = settings.status_socket.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return parse_status(json.loads(text))
    except ValueError:
        return None


def player_pids(proc_root: str = "/proc") -> list[int]:
    pids = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return pids
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "cmdline"), "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"field_player.py") for a in argv[:3]):
            pids.append(int(name))
    return sorted(pids)
