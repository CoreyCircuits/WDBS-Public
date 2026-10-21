"""Era station IDs on Channel 10.

Spec: docs/superpowers/specs/2026-09-28-era-logos-design.md, section 4 (amended 2026-09-28: FS42
brackets every commercial break with a 4 s station-ID bump and never plays one before a block's first
feature, so *every* station ID inside a rotation show's block becomes that show's era ID).

Only an entry's path changes, to <fs42>/wdbs_assets/era/id_<era>.mp4 (wdbs-graphics EraId, 4.000 s).
Duration and content_type ('bump', so the FS42 catch-up patch may still skip one when playout runs
late) stay, so block start/end, features, commercials, weather breaks and total length never move.
Blocks of shows not in the rotation manifest keep the generic ID. A slot whose length differs from the
ID leaves the whole block alone. Same guarded write path as weather breaks (see apply()).

The off switch is compute_off() (`wdbs era-id --off`): it puts the generic station ID back wherever a
future block holds an era ID, whatever else was written since; a sidecar revert only works until
another write touches the same blocks.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import posixpath
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import fs42_adapter as fs
from . import replan as rp
from . import weather_break as wb
from .config import Settings
from .era import ERAS

ERA_ASSET_DIR = "wdbs_assets/era"
LENGTH_TOLERANCE_S = 1e-3


class EraIdRefused(wb.WeatherBreakRefused):
    pass


def id_path(assets_dir: str, era: str) -> str:
    return posixpath.join(str(assets_dir), f"id_{era}.mp4")


def load_ids(assets_dir: str) -> dict[str, wb.Intro]:
    """Measure every era ID (frame-exact). One missing or unmeasurable ID refuses the whole run."""
    out = {}
    for era in ERAS:
        try:
            out[era] = wb.load_intro(id_path(assets_dir, era))
        except wb.WeatherBreakRefused as exc:
            raise EraIdRefused(f"era ID for {era}: {exc}")
    return out


def is_station_id(entry: dict, assets_dir: str) -> bool:
    """A generic FS42 station-ID bump, or an era ID put there by an earlier run."""
    if entry.get("content_type") != "bump":
        return False
    p = str(entry.get("path") or "")
    return "/bump/" in p or p.startswith(posixpath.join(str(assets_dir), "id_"))


@dataclass
class EraIdPlan:
    station: str
    block_id: int
    block_start: str
    block_end: str
    block_title: str
    tag: str
    era: str
    swapped: list[int]
    old_plan_sha: str
    new_plan_json: str = field(repr=False)
    old_plan_json: str = field(repr=False, default="")

    def digest(self) -> str:
        core = [self.block_id, self.old_plan_sha, self.new_plan_json]
        return hashlib.sha256(json.dumps(core).encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("new_plan_json")
        d.pop("old_plan_json")
        return d


def compute(conn, station: fs.Station, *, now: dt.datetime, eras: dict[str, str], ids: dict[str, wb.Intro],
            assets_dir: str, at: dt.datetime | None = None) -> tuple[list[EraIdPlan], list[dict]]:
    """Plans for every future block (>= MIN_LEAD out; only the block at `at` when given) whose station
    IDs aren't yet this show's era ID. Returns (plans, skipped)."""
    wb._check_station(station)
    earliest = now + wb.MIN_LEAD
    plans, skipped = [], []
    for b in fs.get_blocks(conn, station.network_name):
        if b.start < earliest or (at is not None and not b.contains(at)):
            continue
        era = eras.get(b.tag_path or "")
        if era is None:
            continue
        target = ids[era]
        plan = b.plan_paths()
        swap, wrong = [], []
        for i, e in enumerate(plan):
            if not is_station_id(e, assets_dir) or e.get("path") == target.path:
                continue
            (swap if abs(float(e.get("duration") or 0) - target.duration) <= LENGTH_TOLERANCE_S else wrong).append(i)
        if wrong:
            skipped.append({"block_id": b.id, "block_start": b.start.isoformat(sep=" "),
                            "reason": f"station-ID slot(s) {wrong} are not {target.duration:.3f}s long"})
            continue
        if not swap:
            continue
        new_plan = [{**e, "path": target.path} if i in swap else e for i, e in enumerate(plan)]
        plans.append(EraIdPlan(
            station=station.network_name, block_id=b.id, block_start=b.start.isoformat(sep=" "),
            block_end=b.end.isoformat(sep=" "), block_title=b.title, tag=b.tag_path, era=era, swapped=swap,
            old_plan_sha=wb._sha(b.plan_json),
            new_plan_json=json.dumps(new_plan), old_plan_json=b.plan_json))
    return plans, skipped


def compute_off(conn, station: fs.Station, *, now: dt.datetime, generic: wb.Intro, assets_dir: str,
                at: dt.datetime | None = None) -> tuple[list[EraIdPlan], list[dict]]:
    """The off switch: put the generic station ID back wherever a future block (>= MIN_LEAD out; only the
    block at `at` when given) holds an era ID. Unlike a sidecar revert it works whatever else was written
    since (weather breaks, replans). Same guarded rehearse/apply as compute()."""
    wb._check_station(station)
    earliest = now + wb.MIN_LEAD
    id_prefix = posixpath.join(str(assets_dir), "id_")
    plans, skipped = [], []
    for b in fs.get_blocks(conn, station.network_name):
        if b.start < earliest or (at is not None and not b.contains(at)):
            continue
        plan = b.plan_paths()
        swap, wrong = [], []
        for i, e in enumerate(plan):
            if e.get("content_type") != "bump" or not str(e.get("path") or "").startswith(id_prefix):
                continue
            (swap if abs(float(e.get("duration") or 0) - generic.duration) <= LENGTH_TOLERANCE_S else wrong).append(i)
        if wrong:
            skipped.append({"block_id": b.id, "block_start": b.start.isoformat(sep=" "),
                            "reason": f"station-ID slot(s) {wrong} are not {generic.duration:.3f}s long"})
            continue
        if not swap:
            continue
        new_plan = [{**e, "path": generic.path} if i in swap else e for i, e in enumerate(plan)]
        plans.append(EraIdPlan(
            station=station.network_name, block_id=b.id, block_start=b.start.isoformat(sep=" "),
            block_end=b.end.isoformat(sep=" "), block_title=b.title, tag=b.tag_path or "", era="generic",
            swapped=swap, old_plan_sha=wb._sha(b.plan_json),
            new_plan_json=json.dumps(new_plan), old_plan_json=b.plan_json))
    return plans, skipped


BACKUP_LABEL = "eraid"


def verify(settings: Settings, db: Path, plans: list[EraIdPlan], before: dict[int, str]) -> list[dict]:
    out = []

    def check(name, ok, detail=""):
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    targets = {p.block_id for p in plans}
    with fs.connect_ro(db) as conn:
        after = {b.id: b for b in fs.get_blocks(conn, plans[0].station)}
    changed = [i for i, fp in before.items() if i not in targets and (i not in after or after[i].fingerprint() != fp)]
    check("all other blocks untouched", not changed,
          f"{len(before) - len(targets)} identical" if not changed else str(changed[:10]))
    lc = wb._fs42_load_check(settings, db, sorted(targets))
    bad = {k: [] for k in ("present", "times", "written", "paths", "ids", "fs42")}
    lengths: dict[str, float | None] = {}
    for p in plans:
        b = after.get(p.block_id)
        if b is None:
            bad["present"].append(p.block_id)
            continue
        if (b.start.isoformat(sep=" "), b.end.isoformat(sep=" "), b.title) != (p.block_start, p.block_end, p.block_title):
            bad["times"].append(p.block_id)
        if b.plan_json != p.new_plan_json:
            bad["written"].append(p.block_id)
        old, new = json.loads(p.old_plan_json), b.plan_paths()
        same_shape = len(old) == len(new) and all(
            o == n if i not in p.swapped else {**o, "path": n.get("path")} == n
            for i, (o, n) in enumerate(zip(old, new)))
        if not same_shape:
            bad["paths"].append(p.block_id)
        for i in p.swapped:
            path = new[i].get("path") if i < len(new) else None
            if path:
                if path not in lengths:
                    lengths[path] = wb._measure(path)
                got = lengths[path]
            else:
                got = None
            if got is None or abs(got - float(new[i].get("duration") or 0)) > LENGTH_TOLERANCE_S:
                bad["ids"].append(p.block_id)
                break
        loaded = (lc.get("blocks") or {}).get(p.block_id)
        total = sum(float(e.get("duration") or 0) for e in old)
        if (not loaded or loaded["entries"] != len(old) or loaded["web"] != sum(1 for e in old if wb.is_web_entry(e))
                or abs(loaded["total"] - total) >= 1e-6):
            bad["fs42"].append(p.block_id)
    n = len(plans)
    check("target blocks present", not bad["present"], f"{n} block(s)" if not bad["present"] else str(bad["present"]))
    check("block times and titles unchanged", not bad["times"], str(bad["times"] or ""))
    check("plans written exactly", not bad["written"], str(bad["written"] or ""))
    check("only station-ID paths changed (lengths, features, commercials, weather identical)", not bad["paths"],
          str(bad["paths"] or ""))
    check("era ID files present, lengths match the schedule", not bad["ids"],
          str(bad["ids"]) if bad["ids"] else f"{sum(len(p.swapped) for p in plans)} ID(s)")
    check("FS42 loads every changed block", lc.get("ok") and not bad["fs42"],
          lc.get("error") or (str(bad["fs42"]) if bad["fs42"] else f"{n} block(s)"))
    return out


def rehearse(settings: Settings, plans: list[EraIdPlan]) -> dict:
    if not plans:
        return {"ok": True, "checks": []}
    with tempfile.TemporaryDirectory(prefix="wdbs-eraid-") as tmp:
        copy = Path(tmp) / "fs42_fluid.db"
        rp.sqlite_copy(settings.fs42_db, copy)
        before = wb._snapshot(copy, plans[0].station)
        wb._write_all(copy, [(p.block_id, p.old_plan_json, p.new_plan_json) for p in plans])
        checks = verify(settings, copy, plans, before)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def apply(settings: Settings, plans: list[EraIdPlan], now: dt.datetime) -> dict:
    """Write under the FS42 write lock; the sidecar works with weather_break.revert()."""
    if not plans:
        return {"ok": True, "checks": [], "note": "nothing to do"}
    if any(fs.parse_ts(p.block_start) - now < wb.MIN_LEAD for p in plans):
        raise EraIdRefused("A target block is now on air or starts too soon; re-run the plan.")

    def run():
        live = settings.fs42_db
        backup, sidecar = wb._backup(settings, BACKUP_LABEL, {"changes": [
            {"plan": p.as_dict(), "old_plan_json": p.old_plan_json, "new_plan_json": p.new_plan_json} for p in plans]})
        before = wb._snapshot(live, plans[0].station)
        try:
            wb._write_all(live, [(p.block_id, p.old_plan_json, p.new_plan_json) for p in plans])
        except Exception as exc:
            # all-or-nothing transaction: the live DB is untouched, so no restore
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
            wb.prune_backups(settings, label=BACKUP_LABEL)
        return {"ok": ok, "backup": str(backup), "sidecar": str(sidecar), "restored": not ok, "checks": checks}

    return wb._guarded(settings, run)
