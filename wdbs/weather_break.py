"""Weather break: put one timed WeatherStar segment into a future break.

FS42's player plays any plan entry whose path is ``:autobump:=<url>`` as a
web page for exactly that entry's duration, then closes it and moves on
(station_player.play_file / _play_from_point). This module swaps a run of
consecutive commercials inside one break of a *future* block for a single
such entry of exactly their combined duration, so every block keeps its
start, end, feature segments and total length.

Dry run by default (on a DB copy). Apply mirrors replan: refuses while the
overnight job runs, takes the WDBS write lock, backs up the FS42 DB, writes
one row with compare-and-swap, verifies, and restores the backup on any
failure. A sidecar JSON next to the backup records the old plan so
``revert`` can put the commercials back.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import fs42_adapter as fs
from . import replan as rp
from .config import Settings

WEB_PREFIX = ":autobump:="
# Intro + WeatherStar are one unit; the FS42 catch-up patch skips a "weather" group whole or not at all.
WEATHER_TYPE = "weather"
MIN_LEAD = dt.timedelta(minutes=10)
MIN_SEGMENT_S = 40.0
MAX_SEGMENT_S = 95.0


class WeatherBreakRefused(Exception):
    pass


def is_web_entry(entry: dict) -> bool:
    return str(entry.get("path") or "").startswith(WEB_PREFIX)


def breaks_of(plan: list[dict]) -> list[tuple[int, int]]:
    """(start, end) index ranges of each run of non-feature entries after a feature segment."""
    out, i, n = [], 0, len(plan)
    seen_feature = False
    while i < n:
        if plan[i].get("content_type", "feature") == "feature":
            seen_feature = True
            i += 1
            continue
        j = i
        while j < n and plan[j].get("content_type", "feature") != "feature":
            j += 1
        if seen_feature:
            out.append((i, j))
        i = j
    return out


def _best_run(plan: list[dict], lo: int, hi: int, target: float,
              min_s: float = MIN_SEGMENT_S, max_s: float = MAX_SEGMENT_S) -> tuple[int, int] | None:
    """Contiguous run of commercial entries in [lo, hi) whose total is closest to target."""
    best = None
    for a in range(lo, hi):
        total = 0.0
        for b in range(a, hi):
            if plan[b].get("content_type") != "commercial":
                break
            total += float(plan[b].get("duration") or 0)
            if min_s <= total <= max_s:
                score = abs(total - target)
                if best is None or score < best[0]:
                    best = (score, a, b + 1)
    return (best[1], best[2]) if best else None


@dataclass
class WeatherBreakPlan:
    station: str
    block_id: int
    block_start: str
    block_end: str
    block_title: str
    break_number: int
    first_index: int
    replaced: list[dict]
    segment_s: float
    airs_at: str
    url: str
    old_plan_sha: str
    new_plan_json: str = field(repr=False)
    old_plan_json: str = field(repr=False, default="")
    inserted: int = 1          # entries put in place of `replaced` (2 when an intro leads the segment)
    intro: str = ""            # intro file path, when one leads the segment

    def digest(self) -> str:
        core = [self.block_id, self.old_plan_sha, self.new_plan_json]
        return hashlib.sha256(json.dumps(core).encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("new_plan_json")
        d.pop("old_plan_json")
        return d


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class Intro:
    path: str
    duration: float


def load_intro(path: str) -> Intro:
    """Measure an intro clip: exact frames / fps of its video stream."""
    if not os.path.isfile(path):
        raise WeatherBreakRefused(f"Intro file not found: {path}")
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=nb_read_packets,r_frame_rate", "-of", "json", path],
                       capture_output=True, text=True, timeout=60)
    try:
        s = json.loads(r.stdout)["streams"][0]
        num, den = (int(x) for x in s["r_frame_rate"].split("/"))
        duration = int(s["nb_read_packets"]) * den / num
    except (KeyError, IndexError, ValueError, ZeroDivisionError):
        raise WeatherBreakRefused(f"Could not measure intro {path}: {(r.stderr or r.stdout).strip()[-200:]}")
    if not 1.0 <= duration <= 10.0:
        raise WeatherBreakRefused(f"Intro {path} is {duration:.2f}s; expected 1-10 s.")
    return Intro(path, round(duration, 6))


def _intro_entry(intro: Intro) -> dict:
    return {"path": intro.path, "skip": 0, "duration": intro.duration, "is_stream": False,
            "content_type": WEATHER_TYPE, "media_type": "video"}


MORNING_HOURS = range(5, 11)   # weather breaks airing 05:00-10:59 lead with the morning bumper


def pick_intro(intro: "Intro | None", morning: "Intro | None", airs_at: dt.datetime) -> "Intro | None":
    return morning if (morning is not None and airs_at.hour in MORNING_HOURS) else intro


def _plan_for(station: str, block: fs.Block, plan: list[dict], url: str, break_number: int,
              run: tuple[int, int], intro: Intro | None = None) -> WeatherBreakPlan:
    a, b = run
    replaced = plan[a:b]
    segment = sum(float(e.get("duration") or 0) for e in replaced)
    web_s = segment - (intro.duration if intro else 0.0)
    entry = {"path": WEB_PREFIX + url, "skip": 0, "duration": web_s, "is_stream": False,
             "content_type": WEATHER_TYPE, "media_type": "video"}
    inserted = ([_intro_entry(intro)] if intro else []) + [entry]
    new_plan = plan[:a] + inserted + plan[b:]
    return WeatherBreakPlan(
        station=station, block_id=block.id,
        block_start=block.start.isoformat(sep=" "), block_end=block.end.isoformat(sep=" "),
        block_title=block.title, break_number=break_number, first_index=a,
        replaced=[{"file": os.path.basename(e["path"]), "duration": e.get("duration")} for e in replaced],
        segment_s=round(segment, 3), airs_at=_offset(block, plan, a).isoformat(sep=" ", timespec="seconds"),
        url=url, old_plan_sha=_sha(block.plan_json), new_plan_json=json.dumps(new_plan),
        old_plan_json=block.plan_json, inserted=len(inserted), intro=intro.path if intro else "")


def _offset(block: fs.Block, plan: list[dict], index: int) -> dt.datetime:
    return block.start + dt.timedelta(seconds=sum(float(e.get("duration") or 0) for e in plan[:index]))


def _check_station(station: fs.Station):
    if station.network_type != "standard":
        raise WeatherBreakRefused(f"{station.network_name} is a {station.network_type} station.")


def compute_plan(conn, station: fs.Station, *, now: dt.datetime, at: dt.datetime, url: str,
                 break_number: int = 1, target_s: float = 60.0, intro: Intro | None = None,
                 morning_intro: Intro | None = None) -> WeatherBreakPlan:
    _check_station(station)
    blocks = fs.get_blocks(conn, station.network_name)
    block = fs.block_at(blocks, at)
    if block is None:
        raise WeatherBreakRefused(f"No {station.network_name} block at {at}.")
    if block.start - now < MIN_LEAD:
        raise WeatherBreakRefused(f"Block '{block.title}' starts {block.start}; weather breaks go only into "
                                  f"blocks starting at least {int(MIN_LEAD.total_seconds() // 60)} min from now.")
    plan = block.plan_paths()
    if any(is_web_entry(e) for e in plan):
        raise WeatherBreakRefused(f"Block {block.id} already has a web entry.")
    brks = breaks_of(plan)
    if not 1 <= break_number <= len(brks):
        raise WeatherBreakRefused(f"Block {block.id} has {len(brks)} break(s); break {break_number} does not exist.")
    lo, hi = brks[break_number - 1]
    run = _best_run(plan, lo, hi, target_s)
    if run is None:
        raise WeatherBreakRefused(f"Break {break_number} has no run of commercials totalling "
                                  f"{MIN_SEGMENT_S:.0f}-{MAX_SEGMENT_S:.0f}s.")
    return _plan_for(station.network_name, block, plan, url, break_number, run,
                     pick_intro(intro, morning_intro, _offset(block, plan, run[0])))


# --------------------------------------------------------------------------
# Hourly rule: one segment per clock hour, in the break nearest :08
# --------------------------------------------------------------------------

HOURLY_ANCHOR = dt.timedelta(minutes=8)
HOURLY_MIN_S = 45.0
HOURLY_MAX_S = 80.0


def compute_hourly(conn, station: fs.Station, *, now: dt.datetime, url: str, target_s: float = 60.0,
                   until: dt.datetime | None = None, intro: Intro | None = None,
                   morning_intro: Intro | None = None) -> tuple[list[WeatherBreakPlan], list[dict]]:
    """One weather break per clock hour from now+MIN_LEAD to the schedule horizon (or until).

    Hours that already have a web entry are left alone, so re-running is a no-op.
    Returns (plans, skipped) where skipped explains hours that got nothing.
    """
    _check_station(station)
    blocks = fs.get_blocks(conn, station.network_name)
    earliest = now + MIN_LEAD
    horizon = min(until, blocks[-1].end) if (until and blocks) else (blocks[-1].end if blocks else now)
    by_hour: dict[dt.datetime, list[fs.Block]] = {}
    for b in blocks:
        by_hour.setdefault(b.start.replace(minute=0, second=0, microsecond=0), []).append(b)
    plans, skipped = [], []
    for hour in sorted(by_hour):
        if hour + dt.timedelta(hours=1) <= earliest or hour >= horizon:
            continue
        hour_blocks = by_hour[hour]
        if any(is_web_entry(e) for b in hour_blocks for e in b.plan_paths()):
            continue
        candidates = []
        for b in hour_blocks:
            if b.start < earliest or b.end > horizon:
                continue
            plan = b.plan_paths()
            for n, (lo, hi) in enumerate(breaks_of(plan), start=1):
                run = _best_run(plan, lo, hi, target_s, HOURLY_MIN_S, HOURLY_MAX_S)
                if run:
                    at = _offset(b, plan, run[0])
                    candidates.append((abs((at - (hour + HOURLY_ANCHOR)).total_seconds()), b, plan, n, run))
        if not candidates:
            skipped.append({"hour": hour.isoformat(sep=" ", timespec="minutes"),
                            "reason": "no eligible block or no 45-80s run of commercials"})
            continue
        _, b, plan, n, run = min(candidates, key=lambda c: c[0])
        plans.append(_plan_for(station.network_name, b, plan, url, n, run,
                               pick_intro(intro, morning_intro, _offset(b, plan, run[0]))))
    return plans, skipped


def compute_intro_upgrade(conn, station: fs.Station, *, now: dt.datetime, intro: Intro,
                          at: dt.datetime | None = None, url: str | None = None,
                          morning_intro: Intro | None = None) -> list[WeatherBreakPlan]:
    """Put the intro in front of weather breaks already scheduled in future blocks.

    The WeatherStar entry is shortened by the intro's length, so block timing is unchanged.
    When `url` is given, the WeatherStar entry is also pointed at it (e.g. after a ticker/branding change).
    Blocks that already lead with this intro are left alone (re-running is a no-op).
    """
    _check_station(station)
    earliest = now + MIN_LEAD
    plans = []
    for b in fs.get_blocks(conn, station.network_name):
        if b.start < earliest or (at is not None and not b.contains(at)):
            continue
        plan = b.plan_paths()
        idx = [i for i, e in enumerate(plan) if is_web_entry(e)]
        if len(idx) != 1:
            continue
        i = idx[0]
        block_intro = pick_intro(intro, morning_intro, _offset(b, plan, i))
        if i > 0 and plan[i - 1].get("path") in (intro.path, morning_intro.path if morning_intro else None):
            continue
        web = plan[i]
        d = float(web.get("duration") or 0)
        if d - block_intro.duration < 30:
            continue
        new_web = {**web, "duration": d - block_intro.duration, "content_type": WEATHER_TYPE}
        if url:
            new_web["path"] = WEB_PREFIX + url
        new_plan = plan[:i] + [_intro_entry(block_intro), new_web] + plan[i + 1:]
        brk = next((n for n, (lo, hi) in enumerate(breaks_of(plan), start=1) if lo <= i < hi), 0)
        plans.append(WeatherBreakPlan(
            station=station.network_name, block_id=b.id,
            block_start=b.start.isoformat(sep=" "), block_end=b.end.isoformat(sep=" "),
            block_title=b.title, break_number=brk, first_index=i,
            replaced=[{"file": "WeatherStar segment", "duration": d}],
            segment_s=round(d, 3), airs_at=_offset(b, plan, i).isoformat(sep=" ", timespec="seconds"),
            url=str(new_web["path"])[len(WEB_PREFIX):], old_plan_sha=_sha(b.plan_json),
            new_plan_json=json.dumps(new_plan), old_plan_json=b.plan_json, inserted=2, intro=block_intro.path))
    return plans


def compute_asap(conn, station: fs.Station, *, now: dt.datetime, url: str, target_s: float = 60.0,
                 intro: Intro | None = None, morning_intro: Intro | None = None) -> WeatherBreakPlan:
    """The soonest break (in a block starting >= MIN_LEAD from now, without a weather break) that can take one.

    Used for severe-weather alerts: an extra segment as early as the guarded write path allows.
    """
    _check_station(station)
    earliest = now + MIN_LEAD
    for b in fs.get_blocks(conn, station.network_name):
        if b.start < earliest:
            continue
        plan = b.plan_paths()
        if any(is_web_entry(e) for e in plan):
            continue
        for n, (lo, hi) in enumerate(breaks_of(plan), start=1):
            run = _best_run(plan, lo, hi, target_s, HOURLY_MIN_S, HOURLY_MAX_S)
            if run:
                return _plan_for(station.network_name, b, plan, url, n, run,
                                 pick_intro(intro, morning_intro, _offset(b, plan, run[0])))
    raise WeatherBreakRefused("No upcoming block without a weather break has a 45-80 s run of commercials.")


def batch_digest(plans: list[WeatherBreakPlan]) -> str:
    return hashlib.sha256(json.dumps([p.digest() for p in plans]).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Write + verify (same code for rehearsal copy and live DB)
# --------------------------------------------------------------------------

def _write_all(db: Path, changes: list[tuple[int, str, str]]):
    """changes: (block_id, expected plan_json, new plan_json). One transaction, compare-and-swap."""
    conn = sqlite3.connect(str(db), timeout=60)
    try:
        for block_id, expect_json, new_json in changes:
            cur = conn.execute("UPDATE liquid_blocks SET plan_json = ? WHERE id = ? AND plan_json = ?",
                               (new_json, block_id, expect_json))
            if cur.rowcount != 1:
                conn.rollback()
                raise WeatherBreakRefused(f"Block {block_id} changed since the plan was made; nothing written.")
        conn.commit()
    finally:
        conn.close()


def _fs42_load_check(settings: Settings, db: Path, block_ids: list[int]) -> dict:
    """Load blocks with FS42's own LiquidIO in a child process (pinned to db)."""
    code = (
        "import json,os,sys,sqlite3\n"
        "root,db=sys.argv[1],sys.argv[2]; ids=json.loads(sys.argv[3]); os.chdir(root); sys.path.insert(0,root)\n"
        "from fs42.station_manager import StationManager\n"
        "StationManager().server_conf['db_path']=db\n"
        "from fs42.liquid_io import LiquidIO\n"
        "c=sqlite3.connect(db); out={}\n"
        "for bid in ids:\n"
        "    b=LiquidIO._build_block_from_row(c.execute('SELECT * FROM liquid_blocks WHERE id=?',(bid,)).fetchone())\n"
        "    out[bid]={'entries':len(b.plan),'web':sum(1 for e in b.plan if e.path.startswith(':autobump:=')),"
        "'total':sum(e.duration for e in b.plan)}\n"
        "print(json.dumps(out))\n")
    r = subprocess.run([sys.executable, "-c", code, str(settings.fs42_root), str(db), json.dumps(block_ids)],
                       capture_output=True, text=True, cwd=str(settings.fs42_root), timeout=300)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[-400:]}
    return {"ok": True, "blocks": {int(k): v for k, v in json.loads(r.stdout.strip().splitlines()[-1]).items()}}


def verify(settings: Settings, db: Path, plans: list[WeatherBreakPlan], before: dict[int, str]) -> list[dict]:
    out = []

    def check(name, ok, detail=""):
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    targets = {p.block_id for p in plans}
    station = plans[0].station
    with fs.connect_ro(db) as conn:
        after = {b.id: b for b in fs.get_blocks(conn, station)}
    changed = [i for i, fp in before.items() if i not in targets and (i not in after or after[i].fingerprint() != fp)]
    check("all other blocks untouched", not changed,
          f"{len(before) - len(targets)} identical" if not changed else str(changed[:10]))
    lc = _fs42_load_check(settings, db, sorted(targets))
    bad = {k: [] for k in ("present", "times", "written", "outside", "length", "features", "fs42", "intro")}
    intro_lengths: dict[str, float | None] = {}
    for p in plans:
        b = after.get(p.block_id)
        if b is None:
            bad["present"].append(p.block_id)
            continue
        if (b.start.isoformat(sep=" "), b.end.isoformat(sep=" "), b.title) != (p.block_start, p.block_end, p.block_title):
            bad["times"].append(p.block_id)
        if b.plan_json != p.new_plan_json:
            bad["written"].append(p.block_id)
        old, new, n, k = json.loads(p.old_plan_json), b.plan_paths(), len(p.replaced), p.inserted
        if not (new[:p.first_index] == old[:p.first_index] and new[p.first_index + k:] == old[p.first_index + n:]):
            bad["outside"].append(p.block_id)
        if p.intro:
            lead = new[p.first_index] if len(new) > p.first_index else {}
            measured = intro_lengths.setdefault(p.intro, _measure(p.intro))
            if lead.get("path") != p.intro or measured is None or abs(measured - float(lead.get("duration") or 0)) > 1e-3:
                bad["intro"].append(p.block_id)
        tot_old = sum(float(e.get("duration") or 0) for e in old)
        if abs(tot_old - sum(float(e.get("duration") or 0) for e in new)) >= 1e-6:
            bad["length"].append(p.block_id)
        is_feat = lambda e: e.get("content_type", "feature") == "feature"
        if [e for e in new if is_feat(e)] != [e for e in old if is_feat(e)]:
            bad["features"].append(p.block_id)
        got = (lc.get("blocks") or {}).get(p.block_id)
        if not got or got["web"] != 1 or abs(got["total"] - tot_old) >= 1e-6:
            bad["fs42"].append(p.block_id)
    n = len(plans)
    check("target blocks present", not bad["present"], f"{n} block(s)" if not bad["present"] else str(bad["present"]))
    check("block times and titles unchanged", not bad["times"], str(bad["times"] or ""))
    check("plans written exactly", not bad["written"], str(bad["written"] or ""))
    check("entries outside each swap identical", not bad["outside"], str(bad["outside"] or ""))
    check("block play lengths unchanged", not bad["length"], str(bad["length"] or ""))
    check("feature segments unchanged", not bad["features"], str(bad["features"] or ""))
    if any(p.intro for p in plans):
        check("intro file present, length matches the schedule", not bad["intro"],
              str(bad["intro"]) if bad["intro"] else f"{sum(1 for p in plans if p.intro)} block(s)")
    check("FS42 loads every changed block", lc.get("ok") and not bad["fs42"],
          lc.get("error") or (str(bad["fs42"]) if bad["fs42"] else f"{n} block(s), 1 web entry each"))
    return out


def _measure(path: str) -> float | None:
    try:
        return load_intro(path).duration
    except WeatherBreakRefused:
        return None


def _snapshot(db: Path, station: str) -> dict[int, str]:
    with fs.connect_ro(db) as conn:
        return {b.id: b.fingerprint() for b in fs.get_blocks(conn, station)}


def rehearse(settings: Settings, plans: WeatherBreakPlan | list[WeatherBreakPlan]) -> dict:
    plans = plans if isinstance(plans, list) else [plans]
    if not plans:
        return {"ok": True, "checks": []}
    with tempfile.TemporaryDirectory(prefix="wdbs-weather-") as tmp:
        copy = Path(tmp) / "fs42_fluid.db"
        rp.sqlite_copy(settings.fs42_db, copy)
        before = _snapshot(copy, plans[0].station)
        _write_all(copy, [(p.block_id, p.old_plan_json, p.new_plan_json) for p in plans])
        checks = verify(settings, copy, plans, before)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def overnight_state() -> str:
    """'clear' only when systemd positively reports wdbs-overnight.service as not running."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "wdbs-overnight.service"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown ({e})"
    state = r.stdout.strip()
    if state in ("inactive", "failed"):
        return "clear"
    if state in ("active", "activating", "deactivating", "reloading", "refreshing"):
        return "running"
    return f"unknown ({state or r.stderr.strip() or 'no answer'})"


def _guarded(settings: Settings, fn):
    """Take the FS42 write lock first, then refuse unless the overnight job is known to be idle (fail closed)."""
    try:
        with rp.write_lock(settings):
            state = overnight_state()
            if state != "clear":
                raise WeatherBreakRefused(f"wdbs-overnight.service is {state}; not writing. Try again after it finishes.")
            return fn()
    except rp.ReplanRefused as e:
        raise WeatherBreakRefused(str(e))


def _backup(settings: Settings, label: str, sidecar_data: dict) -> tuple[Path, Path]:
    live = settings.fs42_db
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = settings.backups / f"{live.name}.wdbs-{label}-{stamp}.bak"
    rp.sqlite_copy(live, backup)
    sidecar = backup.with_suffix(".json")
    sidecar.write_text(json.dumps(sidecar_data, indent=1))
    return backup, sidecar


BACKUP_KEEP_DAYS = 3   # longer than the schedule horizon, so any not-yet-aired change can still be reverted
BACKUP_KEEP_MIN = 5


def prune_backups(settings: Settings, now: dt.datetime | None = None, label: str = "weather") -> list[str]:
    """Delete <label> backups (.bak + .json sidecar) older than BACKUP_KEEP_DAYS, always keeping the newest few."""
    now = now or dt.datetime.now()
    live = settings.fs42_db
    baks = sorted(settings.backups.glob(f"{live.name}.wdbs-{label}-*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for bak in baks[BACKUP_KEEP_MIN:]:
        if now - dt.datetime.fromtimestamp(bak.stat().st_mtime) > dt.timedelta(days=BACKUP_KEEP_DAYS):
            for f in (bak, bak.with_suffix(".json")):
                if f.exists():
                    f.unlink()
                    removed.append(f.name)
    return removed


def apply(settings: Settings, plans: WeatherBreakPlan | list[WeatherBreakPlan], now: dt.datetime) -> dict:
    plans = plans if isinstance(plans, list) else [plans]
    if not plans:
        return {"ok": True, "checks": [], "note": "nothing to do"}
    if any(fs.parse_ts(p.block_start) - now < MIN_LEAD for p in plans):
        raise WeatherBreakRefused("A target block is now on air or starts too soon; re-run the plan.")

    def run():
        live = settings.fs42_db
        backup, sidecar = _backup(settings, "weather", {"changes": [
            {"plan": p.as_dict(), "old_plan_json": p.old_plan_json, "new_plan_json": p.new_plan_json} for p in plans]})
        before = _snapshot(live, plans[0].station)
        try:
            _write_all(live, [(p.block_id, p.old_plan_json, p.new_plan_json) for p in plans])
        except Exception as exc:
            # _write_all commits all or nothing, so a failure here left the live DB untouched: no restore.
            return {"ok": False, "backup": str(backup), "sidecar": str(sidecar), "restored": False,
                    "error": str(exc), "checks": []}
        try:
            checks = verify(settings, live, plans, before)
        except Exception as exc:
            rp.sqlite_restore(backup, live)
            return {"ok": False, "backup": str(backup), "sidecar": str(sidecar), "restored": True,
                    "error": str(exc), "checks": []}
        ok = all(c["ok"] for c in checks)
        if not ok:
            rp.sqlite_restore(backup, live)
        else:
            prune_backups(settings)
        return {"ok": ok, "backup": str(backup), "sidecar": str(sidecar), "restored": not ok, "checks": checks}

    return _guarded(settings, run)


def revert(settings: Settings, sidecar: Path, now: dt.datetime) -> dict:
    """Put the commercials back for every change in a sidecar whose block has not started yet."""
    data = json.loads(Path(sidecar).read_text())
    changes = data["changes"] if "changes" in data else [data]
    todo = [c for c in changes if fs.parse_ts(c["plan"]["block_start"]) - now >= MIN_LEAD]
    skipped = [c["plan"]["block_id"] for c in changes if c not in todo]
    if not todo:
        return {"ok": True, "checks": [], "note": f"nothing to revert (already aired or too close: {skipped})"}

    def run():
        live = settings.fs42_db
        station = todo[0]["plan"]["station"]
        backup, side = _backup(settings, "weather-revert", {"reverting": str(sidecar)})
        before = _snapshot(live, station)
        ids = {c["plan"]["block_id"] for c in todo}
        try:
            _write_all(live, [(c["plan"]["block_id"], c["new_plan_json"], c["old_plan_json"]) for c in todo])
        except Exception as exc:
            return {"ok": False, "backup": str(backup), "restored": False, "error": str(exc), "checks": []}
        try:
            with fs.connect_ro(live) as conn:
                after = {b.id: b for b in fs.get_blocks(conn, station)}
            changed = [i for i, fp in before.items() if i not in ids and after[i].fingerprint() != fp]
            wrong = [c["plan"]["block_id"] for c in todo if after[c["plan"]["block_id"]].plan_json != c["old_plan_json"]]
            checks = [{"check": "all other blocks untouched", "ok": not changed, "detail": str(changed[:10])},
                      {"check": "original plans restored", "ok": not wrong,
                       "detail": f"{len(todo)} block(s)" if not wrong else str(wrong)}]
        except Exception as exc:
            rp.sqlite_restore(backup, live)
            return {"ok": False, "backup": str(backup), "restored": True, "error": str(exc), "checks": []}
        ok = all(c["ok"] for c in checks)
        if not ok:
            rp.sqlite_restore(backup, live)
        out = {"ok": ok, "backup": str(backup), "restored": not ok, "checks": checks}
        if skipped:
            out["note"] = f"left alone (already aired or too close): {skipped}"
        return out

    return _guarded(settings, run)
