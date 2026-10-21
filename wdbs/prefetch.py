"""Media prefetcher: warm the storage path before FS42 opens a file (BUG-001 mitigation).

A few minutes before each plan entry airs, read the byte ranges mpv needs to start
it - the container head, the tail (mkv cues / mp4 moov), and a window at the
planned seek offset - or the whole file when it is small (commercials, bumps).
That pulls the data through CIFS -> app host (archive drive HDD / rclone Drive cache)
into the app host page cache, rclone's VFS cache (full mode, 1 h), and the TV host
page cache, so the open at air time is a cache hit instead of a cold seek on a
busy disk or a Google Drive fetch.

Read-only. Never talks to the player. Runs at idle CPU/IO priority with a
bandwidth cap. Missing upcoming media is reported ~10 minutes before air.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from dataclasses import dataclass, field

from . import fs42_adapter as fs
from .config import Settings
from .state import StateStore
from .weather_break import WEB_PREFIX

LOG = logging.getLogger("wdbs.prefetch")

LOOKAHEAD = dt.timedelta(minutes=10)
REFRESH_AFTER = dt.timedelta(minutes=40)   # re-warm before rclone's 1 h cache age / page-cache eviction
FINAL_TOUCH = dt.timedelta(seconds=120)    # re-touch shortly before air: Sonarr's GB-size copies evict app host cache
FINAL_TOUCH_MIN_AGE = dt.timedelta(minutes=3)
WHOLE_FILE_MAX = 64 * 1024 * 1024          # read small files (ads, bumps) entirely
HEAD = 4 * 1024 * 1024
TAIL = 2 * 1024 * 1024
WINDOW_BEFORE = 1 * 1024 * 1024
WINDOW_AFTER = 8 * 1024 * 1024
CHUNK = 1024 * 1024


@dataclass
class Target:
    path: str
    skip: float
    airs_at: dt.datetime
    content_type: str
    file_duration: float | None = None

    @property
    def key(self) -> tuple:
        return (self.path, int(self.skip // 30))


@dataclass
class Stats:
    runs: int = 0
    files: int = 0
    bytes: int = 0
    seconds: float = 0.0
    slowest: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)


def upcoming_targets(blocks: list[fs.Block], now: dt.datetime, lookahead=LOOKAHEAD,
                     durations: dict[str, float] | None = None) -> list[Target]:
    """Plan entries whose planned start falls in [now - 1 block entry, now + lookahead]."""
    out = []
    horizon = now + lookahead
    for b in blocks:
        if b.end < now or b.start > horizon:
            continue
        mark = b.start
        for e in b.plan_paths():
            dur = float(e.get("duration") or 0)
            end = mark + dt.timedelta(seconds=dur)
            path = e.get("path")
            # only entries not yet started: mpv already has an entry that is on air open
            if path and not e.get("is_stream") and not path.startswith(WEB_PREFIX) and mark >= now - dt.timedelta(seconds=5) and mark <= horizon:
                out.append(Target(path, float(e.get("skip") or 0), mark, e.get("content_type") or "feature",
                                  (durations or {}).get(path)))
            mark = end
    out.sort(key=lambda t: t.airs_at)
    return out


def ranges_for(size: int, skip: float, file_duration: float | None) -> list[tuple[int, int]]:
    """Byte ranges to read for a start at `skip` seconds. Merged and clamped."""
    if size <= WHOLE_FILE_MAX:
        return [(0, size)]
    rs = [(0, HEAD), (max(0, size - TAIL), size)]
    if skip > 0 and file_duration and file_duration > 0:
        pos = int(size * min(skip / file_duration, 1.0))
        rs.append((max(0, pos - WINDOW_BEFORE), min(size, pos + WINDOW_AFTER)))
    else:
        rs.append((0, min(size, HEAD + WINDOW_AFTER)))
    rs.sort()
    merged = []
    for a, b in rs:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


class Prefetcher:
    def __init__(self, settings: Settings, store: StateStore, max_mbps: float = 4.0, clock=dt.datetime.now,
                 home_channel: int = 10):
        self.settings = settings
        self.store = store
        self.max_bps = max_mbps * 1024 * 1024
        self.clock = clock
        self.home_channel = home_channel
        self.done: dict[tuple, dt.datetime] = {}
        self.stats = Stats()
        self._reported_missing: set[str] = set()
        self.final_done: set[tuple] = set()

    def _read(self, path: str, ranges) -> tuple[int, float]:
        total, t0 = 0, time.monotonic()
        fd = os.open(path, os.O_RDONLY)
        try:
            for a, b in ranges:
                off = a
                while off < b:
                    n = min(CHUNK, b - off)
                    data = os.pread(fd, n, off)
                    if not data:
                        break
                    total += len(data)
                    off += len(data)
                    # bandwidth cap
                    budget = total / self.max_bps
                    spent = time.monotonic() - t0
                    if budget > spent:
                        time.sleep(budget - spent)
        finally:
            os.close(fd)
        return total, time.monotonic() - t0

    def _durations(self, conn) -> dict[str, float]:
        out = {}
        for r in conn.execute("SELECT path, duration FROM catalog_entries"):
            out[r[0]] = float(r[1] or 0)
        for r in conn.execute("SELECT path, duration FROM file_meta WHERE duration IS NOT NULL"):
            out.setdefault(r[0], float(r[1]))
        return out

    def run_once(self) -> dict:
        now = self.clock().replace(microsecond=0)
        st = fs.read_status(self.settings)
        wanted = {self.home_channel} | ({st.channel} if st and st.channel is not None else set())
        stations = [s for s in fs.load_stations(self.settings)
                    if s.network_type in ("standard", "loop") and s.channel in wanted]
        targets: list[Target] = []
        with fs.connect_ro(self.settings.fs42_db) as conn:
            durations = self._durations(conn)
            for s in stations:
                blocks = fs.get_blocks(conn, s.network_name, now - dt.timedelta(hours=2), now + LOOKAHEAD)
                targets += upcoming_targets(blocks, now, LOOKAHEAD, durations)
        targets.sort(key=lambda t: t.airs_at)
        run = {"at": now.isoformat(sep=" "), "targets": len(targets), "read": 0, "bytes": 0, "seconds": 0.0,
               "missing": [], "slowest": None}
        for t in targets:
            last = self.done.get(t.key)
            final = (t.airs_at - now) <= FINAL_TOUCH and t.key not in self.final_done
            if last and now - last < REFRESH_AFTER and not (final and now - last >= FINAL_TOUCH_MIN_AGE):
                continue
            try:
                size = os.stat(t.path).st_size
            except OSError:
                run["missing"].append(t.path)
                if t.path not in self._reported_missing:
                    self._reported_missing.add(t.path)
                    self.store.emit("prefetch.missing_media", {"path": t.path, "airs_at": t.airs_at.isoformat(sep=" ")})
                    LOG.warning("Upcoming media missing (airs %s): %s", t.airs_at, t.path)
                continue
            try:
                n, secs = self._read(t.path, ranges_for(size, t.skip, t.file_duration))
            except OSError as exc:
                LOG.warning("Prefetch read failed %s: %s", t.path, exc)
                continue
            self.done[t.key] = now
            if final:
                self.final_done.add(t.key)
            run["read"] += 1
            run["bytes"] += n
            run["seconds"] += secs
            rate = n / secs / 1024 / 1024 if secs > 0 else None
            if run["slowest"] is None or (rate is not None and rate < run["slowest"]["mb_s"]):
                run["slowest"] = {"path": os.path.basename(t.path), "mb_s": round(rate or 0, 2),
                                  "mb": round(n / 1048576, 1), "seconds": round(secs, 2)}
            LOG.info("warmed %-10s %6.1f MB in %5.2fs  airs %s  %s", t.content_type, n / 1048576, secs,
                     t.airs_at.strftime("%H:%M:%S"), os.path.basename(t.path))
        # forget old keys
        cutoff = now - dt.timedelta(hours=3)
        self.done = {k: v for k, v in self.done.items() if v > cutoff}
        self.final_done &= set(self.done)
        run["seconds"] = round(run["seconds"], 2)
        self.stats.runs += 1
        self.stats.files += run["read"]
        self.stats.bytes += run["bytes"]
        self.store.set_live({"prefetch_last": run,
                             "prefetch_totals": {"runs": self.stats.runs, "files": self.stats.files,
                                                 "mb": round(self.stats.bytes / 1048576, 1)}}, when=now)
        return run

    def run(self, interval: float = 30.0, iterations: int | None = None):
        LOG.info("WDBS prefetcher started (lookahead %s, cap %.1f MB/s)", LOOKAHEAD, self.max_bps / 1048576)
        n = 0
        while iterations is None or n < iterations:
            try:
                self.run_once()
            except Exception:
                LOG.exception("prefetch pass failed")
            n += 1
            if iterations is None or n < iterations:
                time.sleep(interval)
