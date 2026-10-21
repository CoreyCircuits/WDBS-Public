#!/usr/bin/env python3
"""Pre-download Drive-backed episodes that Channel 10 will air soon into rclone's VFS cache (app host).

Everybody Hates Chris and Sanford and Son live only on the read-only Google Drive branch of merged-tv.
A cold mid-file seek from Drive takes 15-40 s (start timeouts, slow channel returns). This reads each
Drive-backed feature file airing in the next HOURS fully through the rclone mount, throttled and at
idle priority, so it sits in the NVMe VFS cache (rclone-cloudtv.service, 40 GB / 72 h) before air.

    drive_prewarm.py [--hours 12] [--mbps 12] [--dry-run]

Read-only everywhere: queries the TV host FS42 DB read-only over ssh, only reads files here.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ARCHIVE = Path("/srv/example-archive")
MERGED = ARCHIVE / "MediaAutomation" / "merged-tv"
LOCAL_BRANCH = ARCHIVE / "MediaAutomation" / "tv"
CLOUD = ARCHIVE / "ExampleTV" / "CloudTV"
STATE = Path.home() / ".local" / "state" / "wdbs" / "drive-prewarm.json"
REWARM_AFTER_S = 48 * 3600   # the VFS cache keeps files 72 h; re-read before they could expire

QUERY = r'''
import sqlite3, json, os, datetime as dt
c = sqlite3.connect("file:runtime/fs42_fluid.db?mode=ro", uri=True)
now = dt.datetime.now(); until = now + dt.timedelta(hours=%d)
out = []
for start, plan in c.execute("SELECT start_time, plan_json FROM liquid_blocks WHERE station='EXAMPLE SITCOMS' "
                             "AND end_time > ? AND start_time < ? ORDER BY start_time",
                             (now.strftime("%%Y-%%m-%%d %%H:%%M:%%S"), until.strftime("%%Y-%%m-%%d %%H:%%M:%%S"))):
    for e in json.loads(plan):
        if (e.get("content_type") or "feature") == "feature" and not e["path"].startswith(":"):
            out.append([start, os.path.realpath(e["path"])])
print(json.dumps(out))
'''


def upcoming(hours):
    r = subprocess.run(["ssh", "TV_HOST", "cd /path/to/FieldStation42 && env/bin/python -"], input=QUERY % hours,
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise SystemExit(f"schedule query failed: {r.stderr[-300:]}")
    return json.loads(r.stdout)


def drive_path(tv_real):
    """TV host realpath (/srv/example-media/...) -> the Drive file on the app host, or None if it is local."""
    if not tv_real.startswith("/srv/example-media/"):
        return None
    p = ARCHIVE / tv_real[len("/srv/example-media/"):]
    if CLOUD in p.parents:
        return p
    if MERGED in p.parents:
        rel = p.relative_to(MERGED)
        if (LOCAL_BRANCH / rel).exists():
            return None
        cand = CLOUD / rel
        return cand if cand.exists() else None
    return None


def read_throttled(path, mbps):
    chunk, budget = 4 << 20, mbps * 1e6
    t0, done = time.monotonic(), 0
    with open(path, "rb", buffering=0) as f:
        while True:
            b = f.read(chunk)
            if not b:
                return done
            done += len(b)
            ahead = done / budget - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--mbps", type=float, default=12.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        os.nice(19)
    except OSError:
        pass
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    seen, todo = set(), []
    for start, real in upcoming(a.hours):
        p = drive_path(real)
        if p and str(p) not in seen:
            seen.add(str(p))
            todo.append((start, p))
    now = time.time()
    print(f"{len(todo)} Drive-backed episode(s) airing in the next {a.hours} h", flush=True)
    for start, p in todo:
        fresh = now - state.get(str(p), 0) < REWARM_AFTER_S
        print(f"  {start[5:16]}  {'cached ' if fresh else 'warm   '} {p.name}", flush=True)
        if fresh or a.dry_run:
            continue
        t = time.monotonic()
        n = read_throttled(p, a.mbps)
        print(f"           {n / 1e6:.0f} MB in {time.monotonic() - t:.0f} s", flush=True)
        state[str(p)] = time.time()
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({k: v for k, v in state.items() if now - v < 7 * 86400}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
