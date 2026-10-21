"""WDBS overnight worker: the 04:30 job with the policy engine as its lineup brain.

Same pipeline and safety rules as tools/wdbs_overnight.py (which stays in place
as the rollback). The legacy module's own functions are reused for tag links,
insert-only catalog sync, catalog readiness, sequence scan, and result writing.
What changes:

  * lineup: WDBS policy engine (honours activate/deactivate-show, Notion
    dayparts/weight/min gap/max per day, playout history) instead of
    choose_lineup. `--lineup legacy` keeps choose_lineup as a soft rollback.
  * BUG-003 fix: station configs are reloaded after the template is written,
    so the sequence scan and the append use tonight's lineup (legacy used the
    config cached at process start, i.e. the previous night's lineup).
  * the FS42 DB is backed up before appending and verified after; a failed
    verification restores the backup.
  * after a successful append the live player is asked to reload (no-op with a
    clear message until the player runs the reload patch).

Must run with cwd = FS42 root (the unit's WorkingDirectory) and the FS42 venv.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _legacy(root: Path):
    spec = importlib.util.spec_from_file_location("wdbs_overnight_legacy", root / "tools" / "wdbs_overnight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_template(config_path: Path, slots: dict) -> str:
    """Same file effect as legacy write_generated_template, given precomputed slots."""
    raw = json.loads(config_path.read_text())
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(f"{config_path}.wdbs-autolineup-{stamp}.bak")
    shutil.copy2(config_path, backup)
    station = raw["station_conf"]
    station.setdefault("day_templates", {})
    station["day_templates"]["wdbs_auto"] = slots
    for day in DAYS:
        station[day] = "wdbs_auto"
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    json.loads(tmp.read_text())
    os.replace(tmp, config_path)
    return str(backup)


def ensure_horizon(station_name: str, now: dt.datetime, dry_run: bool):
    """Legacy ensure_schedule_horizon with an explicit clock (for rehearsals)."""
    from fs42.liquid_schedule import LiquidSchedule
    from fs42.station_manager import StationManager
    liquid = LiquidSchedule(StationManager().station_by_name(station_name))
    end = liquid._end_time()
    target = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=2)
    additions = 0
    if dry_run:
        return end, target, additions
    while end is None or end < target:
        if additions >= 3:
            raise RuntimeError("Refusing to append more than 3 days in one run.")
        liquid.add_days(1)
        additions += 1
        end = liquid._end_time()
    return end, target, additions


def main(argv=None) -> int:
    with contextlib.ExitStack() as locks:
        return _main(argv, locks)


def _main(argv, locks) -> int:
    ap = argparse.ArgumentParser(prog="wdbs overnight")
    ap.add_argument("--fs42-root", default=os.environ.get("WDBS_FS42_ROOT", str(Path.home() / "FieldStation42")))
    ap.add_argument("--station", default="EXAMPLE SITCOMS")
    ap.add_argument("--channel", type=int, default=10)
    ap.add_argument("--manifest", default="/srv/example-media/wdbs/control/channel10_rotation.json")
    ap.add_argument("--media-root", default="/srv/example-media/merged-tv")
    ap.add_argument("--probe-timeout", type=int, default=45)
    ap.add_argument("--lineup", choices=["engine", "legacy"], default="engine")
    ap.add_argument("--now", help="clock override for rehearsals, ISO")
    ap.add_argument("--dry-run", action="store_true", help="compute and print only; no writes at all")
    ap.add_argument("--no-links", action="store_true", help="do not create tag symlinks")
    ap.add_argument("--no-catalog-sync", action="store_true", help="catalog sync in dry-run mode")
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--result", default=None)
    ap.add_argument("--report", default=None, help="write a JSON report here")
    a = ap.parse_args(argv)

    root = Path(a.fs42_root).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["WDBS_FS42_ROOT"] = str(root)

    legacy = _legacy(root)
    from fs42.station_manager import StationManager

    from .api import ControlPlane
    from .config import Settings
    from .replan import ReplanRefused, sqlite_copy, sqlite_restore, write_lock
    from .state import StateStore
    from . import player_control

    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now().replace(microsecond=0)
    settings = Settings(fs42_root=root, manifest=Path(a.manifest))
    store = StateStore(settings.state_db)
    report: dict = {"at": now.isoformat(sep=" "), "lineup_source": a.lineup, "fs42_root": str(root),
                    "dry_run": a.dry_run}
    config_path = root / "confs" / "example_sitcoms.json"
    result_path = a.result or str(root / "runtime" / "wdbs_channel10_catalog_result.json")

    def finish(code: int) -> int:
        report["exit_code"] = code
        if a.report:
            Path(a.report).write_text(json.dumps(report, indent=2, default=str))
        if not a.dry_run:
            store.emit("overnight.completed" if code == 0 else "overnight.failed",
                       {k: report.get(k) for k in ("lineup_source", "days_appended", "schedule_end", "error",
                                                   "verify_ok", "reload")}, channel=a.channel, when=now)
        return code

    if not a.dry_run:
        # F1: this run writes the live FS42 DB (catalog sync, sequence scan, append): hold the shared
        # write lock for all of it. Children (catalog sync) inherit it via LOCK_HELD_ENV.
        try:
            locks.enter_context(write_lock(settings, wait_s=1800))
        except ReplanRefused as exc:
            report["error"] = f"{exc} Waited 30 min; nothing written."
            print(report["error"])
            return finish(1)

    manifest = legacy.load_manifest(a.manifest)
    programs = manifest.get("programs", [])
    print(f"Approved programs in manifest: {len(programs)}")
    if not programs:
        print("No programs are opted into rotation. Leaving current FieldStation42 config and schedule untouched.")
        return finish(0)

    raw = legacy.load_raw_config(config_path)
    content_dir = raw["station_conf"]["content_dir"]
    programs = legacy.ensure_tag_links(programs, content_dir, a.media_root, a.dry_run or a.no_links)
    usable = [p for p in programs if p["link_state"] != "missing_source"]
    if not usable:
        report["error"] = "No approved program has reachable media."
        print(report["error"])
        return finish(1)

    rc = legacy.run_catalog_sync(a.station, usable, root / "tools" / "wdbs_catalog_sync.py", a.probe_timeout,
                                 a.dry_run or a.no_catalog_sync)
    if rc not in (0, 2):
        report["error"] = f"Catalog sync failed with exit code {rc}"
        return finish(1)

    readiness = legacy.catalog_readiness(a.station, usable, content_dir)
    ready = [p for p in readiness if p.get("catalog_status") == "Ready"]
    print(f"Catalog-ready rotation programs: {len(ready)}/{len(readiness)}")
    report["readiness"] = {p["tag"]: p.get("catalog_status") for p in readiness}
    if len(ready) < 2:
        if not a.dry_run:
            legacy.write_result(result_path, readiness, [], None, 0, None, False)
        report["error"] = "Fewer than 2 rotation programs are catalog-ready; preserving current lineup."
        print(report["error"])
        return finish(1)

    target_date = now.date() + dt.timedelta(days=1)
    start = dt.datetime.combine(target_date, dt.time())
    if a.lineup == "legacy":
        lineup = legacy.choose_lineup(ready, target_date.isoformat())
    else:
        cp = ControlPlane(settings, store, clock=lambda: now)
        tags = {p["tag"] for p in ready}
        plan = cp.plan_lineup(a.channel, start, start + dt.timedelta(days=1), eligible_tags=tags,
                              assume_sequences=tags, check_media=False)
        lineup = [{"hour": d["hour"], "minute": int(d["start"][14:16]), "start": d["start"],
                   "daypart": d["daypart"], "program": d["program"], "tag": d["tag"],
                   "score": d["score"], "reason": d["reason"], "relaxed": d["relaxed"]}
                  for d in plan["lineup"]]
        excluded = [p for p in plan["programs"] if not p["in_rotation"]]
        if excluded:
            print("Not scheduled: " + "; ".join(f"{p['program']} ({p['inactive_reason']})" for p in excluded))
        report["excluded"] = excluded
    report["lineup"] = lineup
    if a.lineup == "legacy":
        slots = {str(x["hour"]): {"tags": x["tag"], "sequence": x["tag"]} for x in lineup}
    else:  # engine plans half-hours: two shows can share an hour (list-form FS42 slot)
        from .replan import hour_slots_from_lineup
        slots = {str(hour): slot for (day, hour), slot in hour_slots_from_lineup(lineup).items()
                 if day == target_date}

    print(f"\nGenerated lineup for {target_date} ({a.lineup}):")
    for x in lineup:
        print(f"  {x['hour']:02d}:{x.get('minute', 0):02d} {x['daypart']:<12} {x['program']}")

    if a.dry_run:
        return finish(0)

    config_backup = write_template(config_path, slots)
    report["config_backup"] = config_backup
    sm = StationManager()
    sm.load_json_stations()  # BUG-003: make scan + append see tonight's template
    live_slots = {h: s.get("tags") for h, s in (sm.station_by_name(a.station).get(DAYS[target_date.weekday()]) or {}).items()}
    if live_slots != {h: s["tags"] for h, s in slots.items()}:
        shutil.copy2(config_backup, config_path)
        sm.load_json_stations()
        report["error"] = "Reloaded station config does not show the new template; restored config."
        print(report["error"])
        return finish(1)

    # F3: back up before the sequence scan (its cursor/sequence writes are covered too), and restore
    # on any exception in scan/append/verify - FS42 commits cursor advances separately from blocks.
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    db_backup = settings.backups / f"{settings.fs42_db.name}.wdbs-overnight-{stamp}.bak"
    sqlite_copy(settings.fs42_db, db_backup)
    report["db_backup"] = str(db_backup)

    def errors(v):
        return {c["check"] for c in v["checks"] if not c["ok"] and c["level"] == "error"}

    try:
        legacy.update_sequences(a.station, False)
        from .fs42_adapter import connect_ro, get_sequences
        with connect_ro(settings.fs42_db) as conn:
            seqs = get_sequences(conn, a.station)
        missing = sorted({t for t in (x["tag"] for x in lineup) if (t, t) not in seqs})
        if missing:
            shutil.copy2(config_backup, config_path)
            sm.load_json_stations()
            report["error"] = f"No FS42 sequence after scan for: {', '.join(missing)}; restored config, nothing appended."
            print(report["error"])
            return finish(1)

        pre = errors(ControlPlane(settings, store, clock=lambda: now).verify(a.channel, check_media=False))
        report["pre_existing_errors"] = sorted(pre)
        schedule_end, target, additions = ensure_horizon(a.station, now, False)
        report.update(schedule_end=str(schedule_end), days_appended=additions)
        if schedule_end and schedule_end > target + dt.timedelta(days=1):
            print("WARNING: schedule already extends more than one day beyond the rolling target.")

        v = ControlPlane(settings, store, clock=lambda: now).verify(a.channel, check_media=False)
    except Exception as exc:
        sqlite_restore(db_backup, settings.fs42_db)
        shutil.copy2(config_backup, config_path)
        report["error"] = f"Scan/append failed ({type(exc).__name__}: {exc}); FS42 DB and config restored from backup."
        print(report["error"])
        return finish(1)
    new_errors = errors(v) - pre
    report["verify_ok"] = not new_errors
    report["verify"] = [c for c in v["checks"] if not c["ok"]]
    if new_errors:
        sqlite_restore(db_backup, settings.fs42_db)
        report["error"] = "Post-append verification failed; FS42 DB restored from backup."
        print(report["error"], report["verify"])
        return finish(1)

    legacy.write_result(result_path, readiness, lineup, schedule_end, additions, config_backup, False)
    if additions and not a.no_reload:
        rl = player_control.request_reload(settings, station=a.station)
        report["reload"] = {"ok": rl.get("ok"), "error": rl.get("error")}
    print(f"\nSchedule end: {schedule_end}\nDays appended this run: {additions}\nWDBS overnight run complete.")
    return finish(0)


if __name__ == "__main__":
    raise SystemExit(main())
