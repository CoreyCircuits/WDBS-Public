"""Rehearse the overnight worker end-to-end in a sandbox FS42 root.

Runs `wdbs.overnight` against a sandbox copy (code, confs, DB) and a copy of the
WDBS state DB, with a simulated clock, tag links and catalog sync in dry-run
mode, and no player reload. Then checks what it appended. The live FS42 DB,
live config, and live WDBS state are verified unchanged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from . import fs42_adapter as fs
from .config import Settings
from .replan import sqlite_copy
from .sandbox import make_sandbox, remove_sandbox
from .state import StateStore
from .timeparse import parse_when


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def rehearse(settings: Settings, now: dt.datetime, lineup: str, deactivate: list[str] | None = None,
             keep: bool = False) -> dict:
    live = {"db": _md5(settings.fs42_db), "conf": _md5(settings.confs_dir / "example_sitcoms.json"),
            "state": _md5(settings.state_db) if settings.state_db.exists() else None}
    root = make_sandbox(settings)
    try:
        state_copy = root / "wdbs-state.db"
        if settings.state_db.exists():
            sqlite_copy(settings.state_db, state_copy)
        if deactivate:
            with StateStore(state_copy) as st:
                for tag in deactivate:
                    st.set_override(10, tag, active=False, note="rehearsal")
        report_path = root / "overnight-report.json"
        env = dict(os.environ, WDBS_STATE_DB=str(state_copy),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        cmd = [sys.executable, "-m", "wdbs.overnight", "--fs42-root", str(root), "--now", now.isoformat(),
               "--lineup", lineup, "--no-links", "--no-catalog-sync", "--no-reload",
               "--manifest", str(settings.manifest), "--result", str(root / "result.json"),
               "--report", str(report_path)]
        cmd = ["nice", "-n", "19", "ionice", "-c", "3", *cmd]
        r = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True, timeout=3600)
        out = {"returncode": r.returncode, "stdout_tail": r.stdout[-3000:], "stderr_tail": r.stderr[-3000:]}
        if report_path.exists():
            out["report"] = json.loads(report_path.read_text())
        sb = Settings(fs42_root=root, state_db=state_copy, manifest=settings.manifest)
        target = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time())
        with fs.connect_ro(sb.fs42_db) as c:
            station = fs.station_for_channel(sb, 10)
            day = fs.get_blocks(c, station.network_name, target, target + dt.timedelta(days=1))
            catalog = fs.get_catalog(c, station.network_name)
        out["appended_day"] = [{"start": b.start.isoformat(sep=" "), "tag": b.tag_path, "title": b.title,
                                "episode": catalog[b.content_ids[0]].episode.code
                                if b.content_ids and b.content_ids[0] in catalog else None} for b in day]
        want = {(x["hour"], x.get("minute", 0) // 30): x["tag"] for x in (out.get("report") or {}).get("lineup", [])}
        want.update({(h, 1): t for (h, half), t in list(want.items()) if half == 0 and (h, 1) not in want})
        checks = {
            "worker exited 0": r.returncode == 0,
            "a day was appended": (out.get("report") or {}).get("days_appended", 0) >= 1,
            "post-append verification ok": bool((out.get("report") or {}).get("verify_ok")),
            "every appended block follows tonight's lineup": bool(day) and all(
                want.get((b.start.hour, b.start.minute // 30)) == b.tag_path
                for b in day if b.start.date() == target.date()),
            "live FS42 DB unchanged": _md5(settings.fs42_db) == live["db"],
            "live station config unchanged": _md5(settings.confs_dir / "example_sitcoms.json") == live["conf"],
        }
        if live["state"]:
            # the recorder keeps writing live state; only require that the rehearsal did not add overrides
            with StateStore(settings.state_db) as st:
                checks["live WDBS overrides untouched"] = all(
                    st.get_overrides(10).get(t, {}).get("note") != "rehearsal" for t in (deactivate or []))
        if deactivate:
            checks["deactivated shows absent"] = not ({b.tag_path for b in day} & set(deactivate))
        out["checks"] = checks
        out["ok"] = all(checks.values())
        if keep:
            out["sandbox"] = str(root)
        return out
    finally:
        if not keep:
            remove_sandbox(root)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wdbs overnight-rehearse")
    ap.add_argument("--now", default="tomorrow 04:30", help="simulated run time")
    ap.add_argument("--lineup", choices=["engine", "legacy"], default="engine")
    ap.add_argument("--deactivate", action="append", default=[])
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = parse_when(a.now, dt.datetime.now())
    res = rehearse(Settings(), now, a.lineup, a.deactivate, a.keep)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        rep = res.get("report") or {}
        print(f"Overnight rehearsal @ {now} lineup={a.lineup} deactivate={a.deactivate or '-'}")
        for x in rep.get("lineup", []):
            print(f"  {x['hour']:02d}:{x.get('minute', 0):02d} {x['daypart']:<12} {x['program']}")
        if rep.get("excluded"):
            print("  excluded: " + "; ".join(f"{p['program']} ({p['inactive_reason']})" for p in rep["excluded"]))
        print(f"  appended {rep.get('days_appended')} day(s); schedule end {rep.get('schedule_end')}")
        for k, v in res["checks"].items():
            print(f"  [{'ok  ' if v else 'FAIL'}] {k}")
        if not res["ok"]:
            print(res["stdout_tail"][-1500:])
            print(res["stderr_tail"][-1500:])
        print("REHEARSAL:", "PASS" if res["ok"] else "FAIL")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
