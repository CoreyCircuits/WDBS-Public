"""Safe partial schedule regeneration ("replan future").

Unlike FS42's LiquidManager.reset_schedule() (which rewinds sequences from
*today* and calls catalog._write_catalog()), a replan:

  * deletes only liquid_blocks that start at/after an effective cutoff that is
    never earlier than the end of the block on air now;
  * rewinds each affected sequence cursor to exactly the first episode that was
    in the deleted range, so episode order continues unbroken;
  * gives back the play counts FS42 added for the deleted blocks;
  * never writes catalog rows other than those counts, never touches
    sequence_entries, file_meta, or any other table;
  * rebuilds with FS42's own block builder (LiquidSchedule._fluid) so block
    plans, breaks, and commercials are made exactly as FS42 makes them;
  * rehearses the whole thing on a copy of the DB for --dry-run, and verifies
    invariants after a real run, restoring the backup if any check fails.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import fs42_adapter as fs
from .config import Settings

MAX_REBUILD_SPAN = dt.timedelta(days=14)


class ReplanRefused(Exception):
    pass


@dataclass
class SequenceRollback:
    sequence_name: str
    tag_path: str
    sequence_id: int
    cursor_now: int
    cursor_expected: int | None
    restore_to: int
    restore_path: str


@dataclass
class ReplanPlan:
    channel: int
    station: str
    now: str
    requested_cutoff: str
    effective_cutoff: str
    cutoff_reason: str
    rebuild_until: str
    horizon_end: str | None
    current_block: dict | None
    kept_blocks: int
    delete_block_ids: list[int]
    delete_summary: list[dict]
    sequence_rollbacks: list[SequenceRollback]
    count_restores: dict[int, int]
    date_overrides: dict | None = None
    lineup_source: str = "template"
    lineup: list[dict] | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["count_restores"] = {str(k): v for k, v in self.count_restores.items()}
        return d

    def digest(self) -> str:
        core = {k: v for k, v in self.as_dict().items() if k not in ("now", "warnings", "lineup")}
        return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _block_summary(b: fs.Block) -> dict:
    return {"id": b.id, "start": b.start.isoformat(sep=" "), "end": b.end.isoformat(sep=" "),
            "title": b.title, "tag": b.tag_path}


def compute_plan(conn, station: fs.Station, *, now: dt.datetime, cutoff: dt.datetime,
                 until: dt.datetime | None = None) -> ReplanPlan:
    """Read-only. Decide exactly what a replan would change."""
    name = station.network_name
    if station.network_type != "standard":
        raise ReplanRefused(f"{name} is a {station.network_type} station; replan supports standard stations only.")
    if fs.sequence_group_state_count(conn, name):
        raise ReplanRefused("Station uses child-sequence group state; replan v0.1 does not rewind those.")

    blocks = fs.get_blocks(conn, name)
    if not blocks:
        raise ReplanRefused(f"{name} has no schedule.")
    horizon_end = blocks[-1].end
    for a, b in zip(blocks, blocks[1:]):
        if b.start < a.end:
            raise ReplanRefused(f"Existing schedule overlaps: block {a.id} ends {a.end}, block {b.id} starts {b.start}.")

    current = fs.block_at(blocks, now)
    floor = max(cutoff, now)
    reason = "requested cutoff"
    if current is not None and floor < current.end:
        floor = current.end
        reason = f"moved to end of the program on air now ({current.title})"
    inside = next((b for b in blocks if b.start < floor < b.end), None)
    if inside is not None:
        floor = inside.end
        reason = f"moved to end of block in progress at the cutoff ({inside.title})"
    effective = floor

    if effective >= horizon_end:
        raise ReplanRefused(f"Cutoff {effective} is at or past the schedule horizon ({horizon_end}); nothing to replan.")
    until = until or horizon_end
    if until <= effective:
        raise ReplanRefused(f"--until {until} is not after the effective cutoff {effective}.")
    if until - effective > MAX_REBUILD_SPAN:
        raise ReplanRefused(f"Refusing to rebuild more than {MAX_REBUILD_SPAN.days} days in one replan.")

    to_delete = [b for b in blocks if b.start >= effective]
    kept = [b for b in blocks if b.start < effective]
    assert all(not b.contains(now) for b in to_delete)
    assert not kept or kept[-1].end <= effective

    catalog = fs.get_catalog(conn, name)
    sequences = fs.get_sequences(conn, name)
    warnings: list[str] = []

    rollbacks: list[SequenceRollback] = []
    seen = set()
    for b in to_delete:
        if not b.sequence_key:
            continue
        key = (b.sequence_name, b.tag_path)
        if key in seen:
            continue
        seen.add(key)
        seq = sequences.get(key)
        if seq is None:
            raise ReplanRefused(f"Block {b.id} references missing sequence {key}.")
        if seq.parent_tag:
            raise ReplanRefused(f"Sequence {key} is a child sequence; not supported in v0.1.")
        if len(b.content_ids) != 1 or b.content_ids[0] not in catalog:
            raise ReplanRefused(f"Block {b.id} content {b.content_ids} is not a single catalog entry.")
        path = catalog[b.content_ids[0]].path
        idx = seq.index_of(path)
        if idx is None:
            raise ReplanRefused(f"Episode {path} (block {b.id}) is not in sequence {key}.")
        last = [x for x in blocks if x.sequence_key and (x.sequence_name, x.tag_path) == key][-1]
        last_idx = seq.index_of(catalog[last.content_ids[0]].path) if last.content_ids and last.content_ids[0] in catalog else None
        expected = None if last_idx is None else last_idx + 1
        if expected is not None and expected != seq.current_index:
            raise ReplanRefused(
                f"Sequence {key[0]} cursor is {seq.current_index} but the schedule's last episode implies "
                f"{expected}; something else moved it. Investigate before replanning.")
        rollbacks.append(SequenceRollback(key[0], key[1], seq.id, seq.current_index, expected, idx, path))

    counts = Counter()
    for b in to_delete:
        for cid in b.content_ids:
            if cid in catalog:
                counts[cid] += 1
            else:
                warnings.append(f"Block {b.id} references catalog id {cid} which does not exist (pre-existing orphan).")

    return ReplanPlan(
        channel=station.channel, station=name, now=now.isoformat(sep=" "),
        requested_cutoff=cutoff.isoformat(sep=" "), effective_cutoff=effective.isoformat(sep=" "),
        cutoff_reason=reason, rebuild_until=until.isoformat(sep=" "),
        horizon_end=horizon_end.isoformat(sep=" "),
        current_block=_block_summary(current) if current else None,
        kept_blocks=len(kept), delete_block_ids=[b.id for b in to_delete],
        delete_summary=[_block_summary(b) for b in to_delete],
        sequence_rollbacks=rollbacks, count_restores=dict(counts), warnings=warnings)


def hour_slots_from_lineup(decisions: list[dict]) -> dict[tuple[dt.date, int], dict]:
    """Engine lineup (half-hour or hour decisions) -> one FS42 slot per (date, hour).

    FS42 splits an hour whose "tags" is a list: [a, b] plays a at :00-:29 and b at :30-:59.
    "sequence" is the parallel list (FS42 patch "seq-list"), so each show keeps its own
    episode order. Same show in both halves, or only one half planned -> plain slot."""
    halves: dict[tuple[dt.date, int], dict[int, str]] = {}
    for d in decisions:
        start = fs.parse_ts(d["start"])
        halves.setdefault((start.date(), start.hour), {})[0 if start.minute < 30 else 1] = d["tag"]
    out = {}
    for key, h in halves.items():
        tags = [h.get(0, h.get(1)), h.get(1, h.get(0))]
        if tags[0] == tags[1]:
            out[key] = {"tags": tags[0], "sequence": tags[0]}
        else:
            out[key] = {"tags": tags, "sequence": list(tags)}
    return out


def date_overrides_from_lineup(decisions: list[dict]) -> dict:
    """Engine lineup -> FS42 date_overrides ({"September 27": {"6": slot, ...}}).
    Applied in memory only for the rebuild; never written to the config file."""
    out: dict[str, dict] = {}
    for (day, hour), slot in hour_slots_from_lineup(decisions).items():
        key = day.strftime("%B %d").replace(" 0", " ")
        out.setdefault(key, {})[str(hour)] = slot
    return out


# --------------------------------------------------------------------------
# Snapshot + verification
# --------------------------------------------------------------------------

def snapshot(conn, station: str) -> dict:
    blocks = fs.get_blocks(conn, station)
    return {
        "blocks": {b.id: b.fingerprint() for b in blocks},
        "block_times": {b.id: (b.start.isoformat(sep=" "), b.end.isoformat(sep=" ")) for b in blocks},
        "catalog": fs.catalog_fingerprint(conn, station),
        "sequences": fs.sequences_fingerprint(conn, station),
        "cursors": fs.sequence_cursors(conn, station),
        "other": fs.other_tables_fingerprint(conn),
        "counts": dict(conn.execute("SELECT id, count FROM catalog_entries WHERE station = ?", (station,)).fetchall()),
    }


def verify(conn, plan: ReplanPlan, before: dict, *, check_media: bool = True) -> list[dict]:
    """Return a list of {check, ok, detail}. Everything must be ok to commit."""
    out = []

    def check(name, ok, detail=""):
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    name = plan.station
    cutoff = fs.parse_ts(plan.effective_cutoff)
    until = fs.parse_ts(plan.rebuild_until)
    after_blocks = fs.get_blocks(conn, name)
    after_ids = {b.id for b in after_blocks}
    deleted = set(plan.delete_block_ids)

    kept_before = {i: fp for i, fp in before["blocks"].items() if i not in deleted}
    changed = [i for i, fp in kept_before.items() if i not in after_ids or
               next(b for b in after_blocks if b.id == i).fingerprint() != fp]
    check("historical and current blocks untouched", not changed,
          f"{len(kept_before)} kept block(s) identical" if not changed else f"changed/missing: {changed[:10]}")
    check("only planned blocks deleted", not (deleted & after_ids),
          f"{len(deleted)} deleted" if not (deleted & after_ids) else f"still present: {sorted(deleted & after_ids)[:10]}")

    new_blocks = [b for b in after_blocks if b.id not in before["blocks"]]
    check("new blocks start at or after cutoff", all(b.start >= cutoff for b in new_blocks),
          f"{len(new_blocks)} new block(s)")
    check("new schedule begins exactly at cutoff", bool(new_blocks) and new_blocks[0].start == cutoff,
          new_blocks[0].start.isoformat(sep=" ") if new_blocks else "no new blocks")
    gaps = [(a.id, b.id) for a, b in zip(after_blocks, after_blocks[1:]) if b.start != a.end]
    check("schedule contiguous (no gaps/overlaps)", not gaps, f"{len(gaps)} discontinuities {gaps[:5]}")
    check("horizon preserved", bool(after_blocks) and after_blocks[-1].end >= until,
          f"ends {after_blocks[-1].end if after_blocks else None}, needed {until}")

    cat_after = fs.catalog_fingerprint(conn, name)
    check("catalog IDs/paths/durations unchanged", cat_after["hash"] == before["catalog"]["hash"],
          f"{cat_after['rows']} rows")
    seq_after = fs.sequences_fingerprint(conn, name)
    check("sequence definitions unchanged", seq_after["hash"] == before["sequences"]["hash"],
          f"{seq_after['sequences']} sequences / {seq_after['entries']} entries")
    other_after = fs.other_tables_fingerprint(conn)
    diff_tables = [t for t in before["other"] if other_after.get(t) != before["other"][t]]
    check("file_meta/break/chapter tables unchanged", not diff_tables, ", ".join(diff_tables))

    catalog = fs.get_catalog(conn, name)
    orphans = sorted({cid for b in after_blocks for cid in b.content_ids if cid not in catalog})
    pre_orphans = sorted({cid for b in before.get("_blocks_objs", []) for cid in b.content_ids if cid not in catalog})
    check("no orphan catalog references", not orphans or orphans == pre_orphans, f"{orphans[:10]}")

    # Sequence cursors must agree with the last scheduled episode of each sequence.
    sequences = fs.get_sequences(conn, name)
    bad = []
    used = set()
    for key, seq in sequences.items():
        mine = [b for b in after_blocks if b.sequence_key and (b.sequence_name, b.tag_path) == key]
        if not mine:
            if seq.current_index != before["cursors"].get(f"{key[0]}|{key[1]}"):
                bad.append(f"{key[0]} moved without blocks")
            continue
        used.add(key)
        last = mine[-1]
        if last.content_ids and last.content_ids[0] in catalog:
            idx = seq.index_of(catalog[last.content_ids[0]].path)
            if idx is not None and not seq.follows(idx):
                bad.append(f"{key[0]} cursor {seq.current_index} != {idx + 1}")
    check("sequence cursors consistent with schedule", not bad, "; ".join(bad[:5]))

    breaks = []
    for rb in plan.sequence_rollbacks:
        first = next((b for b in new_blocks if b.sequence_key and
                      (b.sequence_name, b.tag_path) == (rb.sequence_name, rb.tag_path)), None)
        if first is not None:
            path = catalog[first.content_ids[0]].path if first.content_ids and first.content_ids[0] in catalog else None
            if path != rb.restore_path:
                breaks.append(f"{rb.sequence_name}: resumed at {os.path.basename(path or '?')} "
                              f"not {os.path.basename(rb.restore_path)}")
    check("episode order continues across cutoff", not breaks, "; ".join(breaks[:5]))

    if check_media:
        missing = []
        for b in new_blocks:
            for entry in b.plan_paths():
                p = entry.get("path")
                if p and not entry.get("is_stream") and not os.path.exists(p):
                    missing.append(p)
        check("all new media present on disk", not missing, f"{len(missing)} missing {missing[:3]}")

    counts_after = dict(conn.execute("SELECT id, count FROM catalog_entries WHERE station = ?", (name,)).fetchall())
    negative = [i for i, c in counts_after.items() if c is not None and c < 0]
    check("play counts non-negative", not negative, str(negative[:10]))
    return out


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def sqlite_copy(src: Path, dst: Path):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def sqlite_restore(backup: Path, live: Path):
    s = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    d = sqlite3.connect(str(live), timeout=60)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


LOCK_HELD_ENV = "WDBS_FS42_LOCK_HELD"


@contextmanager
def write_lock(settings: Settings, wait_s: float = 0):
    """Exclusive lock for every WDBS writer of the live FS42 DB (replan, overnight, weather,
    path migration, catalog sync). Re-entrant for this process and its children: the holder
    exports LOCK_HELD_ENV, so e.g. the overnight job's catalog-sync child does not wait on it."""
    lock_path = settings.state_db.parent / "fs42-write.lock"
    if os.environ.get(LOCK_HELD_ENV) == str(lock_path):
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ReplanRefused("Another WDBS write operation holds the FS42 write lock.")
                time.sleep(2)
        os.environ[LOCK_HELD_ENV] = str(lock_path)
        try:
            yield
        finally:
            os.environ.pop(LOCK_HELD_ENV, None)


def overnight_running() -> bool:
    """True unless systemd positively says the 04:30 unit is idle (fail closed)."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "wdbs-overnight.service"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    return r.stdout.strip() not in ("inactive", "failed")


def apply_rollback(db_path: Path, plan: ReplanPlan):
    """Delete planned blocks, rewind cursors, give back counts - one transaction."""
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = plan.delete_block_ids
        n = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            n += conn.execute(
                f"DELETE FROM liquid_blocks WHERE station = ? AND start_time >= ? AND id IN ({','.join('?' * len(chunk))})",
                [plan.station, plan.effective_cutoff, *chunk]).rowcount
        if n != len(ids):
            raise RuntimeError(f"Expected to delete {len(ids)} blocks, deleted {n}; rolling back.")
        for rb in plan.sequence_rollbacks:
            cur = conn.execute("UPDATE named_sequence SET current_index = ? WHERE id = ? AND current_index = ?",
                               (rb.restore_to, rb.sequence_id, rb.cursor_now))
            if cur.rowcount != 1:
                raise RuntimeError(f"Sequence {rb.sequence_name} cursor changed underneath us; rolling back.")
        for cid, k in plan.count_restores.items():
            conn.execute("UPDATE catalog_entries SET count = MAX(count - ?, 0) WHERE id = ? AND station = ?",
                         (k, cid, plan.station))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_fs42_build(settings: Settings, db_path: Path, plan: ReplanPlan) -> dict:
    """Run FS42's block builder in a child process pinned to db_path."""
    payload = {
        "fs42_root": str(settings.fs42_root),
        "db_path": str(db_path),
        "station": plan.station,
        "start": plan.effective_cutoff,
        "end": plan.rebuild_until,
        "date_overrides": plan.date_overrides,
    }
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    # Lowest CPU/IO priority: the TV host is RAM/IO constrained and the live player shares it (BUG-001).
    cmd = [sys.executable, "-m", "wdbs.fs42_build"]
    if shutil.which("nice") and shutil.which("ionice"):
        cmd = ["nice", "-n", "19", "ionice", "-c", "3", *cmd]
    r = subprocess.run(cmd, input=json.dumps(payload),
                       capture_output=True, text=True, cwd=str(settings.fs42_root), env=env, timeout=1800)
    result = {"returncode": r.returncode, "stderr_tail": r.stderr[-4000:]}
    if r.returncode == 0:
        try:
            result.update(json.loads(r.stdout.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            result["stdout_tail"] = r.stdout[-2000:]
    return result


def _new_blocks(conn, station: str, before_ids) -> list[dict]:
    return [_block_summary(b) for b in fs.get_blocks(conn, station) if b.id not in before_ids]


def rehearse(settings: Settings, plan: ReplanPlan, check_media: bool = True) -> dict:
    """Dry run: do the whole replan on a temporary copy of the FS42 DB."""
    tmpdir = Path(tempfile.mkdtemp(prefix="wdbs-replan-"))
    try:
        copy = tmpdir / "fs42_fluid.db"
        sqlite_copy(settings.fs42_db, copy)
        return _execute_on(settings, copy, plan, check_media)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _execute_on(settings: Settings, db_path: Path, plan: ReplanPlan, check_media: bool,
                progress: dict | None = None) -> dict:
    with fs.connect_ro(db_path) as conn:
        before = snapshot(conn, plan.station)
        before["_blocks_objs"] = fs.get_blocks(conn, plan.station)
        fresh = compute_plan(conn, fs.Station(plan.channel, plan.station, "standard", Path(), {}),
                             now=fs.parse_ts(plan.now), cutoff=fs.parse_ts(plan.requested_cutoff),
                             until=fs.parse_ts(plan.rebuild_until))
    if fresh.delete_block_ids != plan.delete_block_ids or fresh.effective_cutoff != plan.effective_cutoff:
        raise ReplanRefused("The schedule changed since the plan was computed; re-run the plan.")
    apply_rollback(db_path, plan)  # one transaction: all or nothing
    if progress is not None:
        progress["written"] = True
    build = run_fs42_build(settings, db_path, plan)
    with fs.connect_ro(db_path) as conn:
        checks = verify(conn, plan, before, check_media=check_media) if build["returncode"] == 0 else [
            {"check": "FS42 build succeeded", "ok": False, "detail": build.get("stderr_tail", "")[-800:]}]
        new = _new_blocks(conn, plan.station, before["blocks"]) if build["returncode"] == 0 else []
    return {"build": build, "checks": checks, "ok": all(c["ok"] for c in checks), "new_blocks": new,
            "build_warnings": build.get("warnings", [])}


def apply(settings: Settings, plan: ReplanPlan, now: dt.datetime, check_media: bool = True) -> dict:
    """Real run on the live FS42 DB, with backup and automatic restore on failure."""
    if fs.parse_ts(plan.effective_cutoff) <= now:
        raise ReplanRefused("The effective cutoff is already in the past; re-run the plan.")
    with write_lock(settings):
        if overnight_running():
            raise ReplanRefused("wdbs-overnight.service is running or its state is unknown; "
                                "try again after it finishes.")
        live = settings.fs42_db
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = settings.backups / f"{live.name}.wdbs-replan-{stamp}.bak"
        sqlite_copy(live, backup)
        progress = {"written": False}
        try:
            result = _execute_on(settings, live, plan, check_media, progress)
        except Exception as exc:
            # Nothing committed yet (refused before the rollback transaction): leave the DB alone.
            if progress["written"]:
                sqlite_restore(backup, live)
            return {"ok": False, "backup": str(backup), "restored": progress["written"], "error": str(exc),
                    "checks": []}
        result["backup"] = str(backup)
        if not result["ok"]:
            sqlite_restore(backup, live)
            result["restored"] = True
        else:
            result["restored"] = False
        return result
