"""Playout recorder: turns FS42 status-socket writes into airing history.

FS42 rewrites runtime/play_status.socket each time a plan segment starts
(feature segment, commercial, bump). A feature episode airs as several
segments split by breaks, so an airing is the run of feature segments with the
same file on the same channel. Breaks inside that run do not end the airing.

The recorder only reads FS42 state. It never talks to the player.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from . import fs42_adapter as fs
from .config import Settings
from .episodes import parse_episode
from .state import StateStore, now_str

LOG = logging.getLogger("wdbs.recorder")

COMPLETE_FRACTION = 0.9
BLOCK_END_GRACE = dt.timedelta(minutes=3)


class Recorder:
    def __init__(self, settings: Settings, store: StateStore,
                 pid_fn=fs.player_pids, status_fn=None):
        self.settings = settings
        self.store = store
        self.pid_fn = pid_fn
        self.status_fn = status_fn or (lambda: fs.read_status(settings))
        self._catalog_cache: dict[str, bool] = {}
        self._last_interruption = None

    # -- helpers ----------------------------------------------------------

    def _open_airing(self):
        return self.store.conn.execute(
            "SELECT * FROM airings WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()

    def _close(self, airing, ended_at: dt.datetime, reason: str):
        ended = max(fs.parse_ts(airing["started_at"]), ended_at)
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE airings SET ended_at = ?, end_reason = ? WHERE id = ?",
                (now_str(ended), reason, airing["id"]))
        self.store.emit("airing.ended", {
            "airing_id": airing["id"], "tag": airing["tag"], "title": airing["title"],
            "reason": reason, "completed": bool(airing["completed"]),
        }, channel=airing["channel"], when=ended)

    def _in_catalog(self, st: fs.PlayerStatus) -> bool:
        if st.file_path in self._catalog_cache:
            return self._catalog_cache[st.file_path]
        try:
            with fs.connect_ro(self.settings.fs42_db) as conn:
                row = conn.execute("SELECT 1 FROM catalog_entries WHERE path = ? OR realpath = ? LIMIT 1",
                                   (st.file_path, st.file_path)).fetchone()
        except Exception:
            return True  # can't tell; don't drop a real airing
        if len(self._catalog_cache) > 5000:
            self._catalog_cache.clear()
        self._catalog_cache[st.file_path] = row is not None
        return row is not None

    def _position(self, st: fs.PlayerStatus, now: dt.datetime) -> float:
        offset = st.offset_s or 0.0
        if st.timestamp is None:
            return offset
        elapsed = max(0.0, (now - st.timestamp).total_seconds())
        if st.segment_s is not None:
            elapsed = min(elapsed, st.segment_s)
        return offset + elapsed

    def _lookup(self, st: fs.PlayerStatus):
        """Find the catalog entry and scheduled block for a playing file."""
        entry = block = None
        try:
            with fs.connect_ro(self.settings.fs42_db) as conn:
                row = conn.execute(
                    "SELECT id FROM catalog_entries WHERE station = ? AND (path = ? OR realpath = ?) "
                    "ORDER BY id LIMIT 1", (st.network_name, st.file_path, st.file_path)).fetchone()
                if row:
                    entry = fs.get_catalog(conn, st.network_name).get(row[0])
                when = st.timestamp or dt.datetime.now()
                for b in fs.get_blocks(conn, st.network_name, when - dt.timedelta(hours=3),
                                       when + dt.timedelta(minutes=1)):
                    if b.start <= when < b.end or (b.start <= when <= b.end + BLOCK_END_GRACE):
                        if any(p.get("path") == st.file_path for p in b.plan_paths()):
                            block = b
                            break
        except Exception as exc:  # FS42 DB busy/unavailable must not kill the recorder
            LOG.warning("FS42 lookup failed: %s", exc)
        return entry, block

    def _start_airing(self, st: fs.PlayerStatus, now: dt.datetime):
        entry, block = self._lookup(st)
        ep = parse_episode(st.file_path)
        # started_at is when this feature was first seen on air. join_offset_s is how far into
        # the file it started: > 0 means the opening was skipped (late start / catch-up) or the
        # recorder first saw it mid-episode (recorder_restart events tell those apart).
        started = st.timestamp or now
        with self.store.conn:
            cur = self.store.conn.execute(
                "INSERT INTO airings (channel, network, file_path, catalog_id, tag, title, season, episode, "
                "block_id, block_start, block_end, schedule_match, started_at, last_seen_at, "
                "feature_duration_s, position_s, join_offset_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (st.channel, st.network_name, st.file_path,
                 entry.id if entry else None, entry.tag if entry else None,
                 entry.title if entry else st.title, ep.season, ep.episode,
                 block.id if block else None,
                 now_str(block.start) if block else None, now_str(block.end) if block else None,
                 int(block is not None), now_str(started), now_str(now),
                 entry.duration if entry else None, self._position(st, now), st.offset_s or 0.0))
        airing_id = cur.lastrowid
        self.store.emit("airing.started", {
            "airing_id": airing_id, "tag": entry.tag if entry else None,
            "title": entry.title if entry else st.title, "episode": ep.code,
            "schedule_match": block is not None, "join_offset_s": st.offset_s or 0.0,
        }, channel=st.channel, when=started)
        skipped = self._skipped_opening(block, st) if block is not None else 0.0
        if skipped > 5:
            self.store.emit("playout.late_start", {
                "airing_id": airing_id, "block_id": block.id, "skipped_s": round(skipped, 1),
                "title": entry.title if entry else st.title}, channel=st.channel, when=started)
        if block is None:
            LOG.warning("Airing %s does not match the FS42 schedule in the DB "
                        "(player may be running an older in-memory schedule): %s", airing_id, st.file_path)
        return airing_id

    @staticmethod
    def _skipped_opening(block: fs.Block, st: fs.PlayerStatus) -> float:
        """Seconds of the episode's opening that never aired: status offset beyond the planned
        start of the file's first plan segment (FS42 offset = entry.skip + initial_skip)."""
        for entry in block.plan_paths():
            if entry.get("path") == st.file_path:
                return max(0.0, (st.offset_s or 0.0) - float(entry.get("skip") or 0.0))
        return 0.0

    def _touch(self, airing, st: fs.PlayerStatus | None, now: dt.datetime):
        position = airing["position_s"] or 0.0
        if st is not None and st.file_path == airing["file_path"]:
            position = max(position, self._position(st, now))
        duration = airing["feature_duration_s"]
        completed = int(bool(duration) and position >= COMPLETE_FRACTION * duration)
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE airings SET last_seen_at = ?, position_s = ?, completed = MAX(completed, ?) WHERE id = ?",
                (now_str(now), position, completed, airing["id"]))

    # -- main -------------------------------------------------------------

    def poll(self, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now()
        pids = self.pid_fn()
        st = self.status_fn()
        online = bool(pids)
        live_before = self.store.get_live()
        was_online = (live_before.get("player_online") or {}).get("value")

        if was_online is not None and bool(was_online) != online:
            self.store.emit("player.online" if online else "player.offline", {"pids": pids}, when=now)

        airing = self._open_airing()
        action = "idle"

        if not online:
            if airing:
                self._close(airing, fs.parse_ts(airing["last_seen_at"]), "player_offline")
                action = "closed:player_offline"
        elif st is not None and st.status == "playing" and st.file_path:
            with self.store.conn:
                self.store.conn.execute(
                    "INSERT OR IGNORE INTO segments (observed_at, status_ts, status, channel, network, file_path, "
                    "content_type, offset_s, segment_s, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now_str(now), st.timestamp.isoformat(sep=" ") if st.timestamp else None, st.status,
                     st.channel, st.network_name, st.file_path, st.content_type, st.offset_s,
                     st.segment_s, st.title))
            is_feature = (st.content_type or "feature") == "feature"
            if is_feature and not self._in_catalog(st):
                # Standby image / off-catalog file: an interruption, not a program. The open
                # airing stays open so the episode continues as the same airing on recovery.
                if st.key != self._last_interruption:
                    self._last_interruption = st.key
                    self.store.emit("playout.interruption", {
                        "file": st.file_path, "title": st.title,
                        "airing_id": airing["id"] if airing else None}, channel=st.channel, when=st.timestamp or now)
                action = "interruption"
                is_feature = False
            if airing and airing["channel"] != st.channel:
                self._close(airing, st.timestamp or now, "channel_change")
                airing = None
            if is_feature:
                if airing and airing["file_path"] == st.file_path:
                    self._touch(airing, st, now)
                    action = "continue"
                else:
                    if airing:
                        self._close(airing, st.timestamp or now, "next_program")
                    self._start_airing(st, now)
                    action = "started"
            elif airing and action != "interruption":
                block_end = fs.parse_ts(airing["block_end"]) if airing["block_end"] else None
                if block_end and now > block_end + BLOCK_END_GRACE:
                    self._close(airing, block_end, "block_end")
                    action = "closed:block_end"
                else:
                    self._touch(airing, None, now)
                    action = "break"
        elif st is not None and st.status in ("stopped",) and airing:
            self._close(airing, fs.parse_ts(airing["last_seen_at"]), "player_stopped")
            action = "closed:player_stopped"

        live = {
            "player_online": online,
            "player_pids": pids,
            "player_status": st.status if st else None,
            "channel": st.channel if st else None,
            "network": st.network_name if st else None,
            "file_path": st.file_path if st else None,
            "content_type": st.content_type if st else None,
            "title": st.title if st else None,
            "status_ts": st.timestamp.isoformat(sep=" ") if st and st.timestamp else None,
            "recorder_heartbeat": now_str(now),
        }
        open_airing = self._open_airing()
        live["open_airing_id"] = open_airing["id"] if open_airing else None
        self.store.set_live(live, when=now)
        return {"action": action, **live}

    def run(self, interval: float = 5.0, iterations: int | None = None):
        LOG.info("WDBS recorder started (interval=%ss, fs42_db=%s)", interval, self.settings.fs42_db)
        n = 0
        while iterations is None or n < iterations:
            try:
                result = self.poll()
                if result["action"] not in ("continue", "break", "idle"):
                    LOG.info("%s %s", result["action"], result.get("file_path"))
            except Exception:
                LOG.exception("Recorder poll failed")
            n += 1
            if iterations is None or n < iterations:
                time.sleep(interval)
