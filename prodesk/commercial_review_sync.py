#!/usr/bin/env python3
"""WDBS commercial review loop (app host side). wner-approved spots air.

    commercial_review_sync.py stage-review        # copy air masters to the archive drive review folder
                                                  # (the TV host sees it at /srv/example-media/ExampleTV/Commercials/wdbs-review)
    commercial_review_sync.py sync [--apply]      # TV host verdicts -> Notion: approve -> Approved, reject -> Disabled
    commercial_review_sync.py promote [--apply]   # Approved -> copy into Channel 10's commercial dir, catalog on the
                                                  # TV host (insert-only v5 sync, content_type=commercial), Notion -> Live
                                                  # "Late Night Only" rows go to clips/latenight/ (FS42 day-part hint,
                                                  # 22:00-05:00); held if the TV host lacks that day part. Ticking the box
                                                  # on a spot that is already Live does not move it.

Dry run unless --apply. Idempotent. Notion token from the existing integration env (never printed).
"""

import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import commercial_notion_rows as nr  # noqa: E402  (token + Notion call helper)

AIR = Path.home() / "wdbs-media" / "commercials" / "air"
REVIEW = Path("/srv/example-archive/ExampleTV/Commercials/wdbs-review")
# Channel 10 commercial_dir target. Since 2026-09-30 the live ads sit on the app host NVMe, not the archive drive
# USB drive (ads were 60% of start timeouts); the TV host mounts this folder read-only at /srv/example-ads and
# catalog/example_sitcoms/commercial points there. The old archive drive clips/ folder is a frozen copy.
LIVE = Path.home() / "wdbs-media" / "commercials" / "example-live"
TV_LIVE = "/srv/example-ads"
VERDICTS_REMOTE = "TV_HOST:.local/state/wdbs/commercial-review.tsv"


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def asset_id(filename):
    return Path(filename).stem.rsplit("__", 1)[-1]


def rows_by_asset():
    out, cursor = {}, None
    while True:
        r = nr.call("POST", f"/data_sources/{nr.DS}/query", {"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
        for p in r["results"]:
            path = "".join(t.get("plain_text", "") for t in p["properties"]["Filename / Path"].get("rich_text", []))
            if path.endswith(".mp4") and "__" in Path(path).stem:
                sel = p["properties"]["Status"].get("select")
                notes = "".join(t.get("plain_text", "") for t in p["properties"]["Notes"].get("rich_text", []))
                out[asset_id(path)] = {"page": p["id"], "status": sel and sel["name"], "path": path, "notes": notes,
                                       "rotation": p["properties"]["In Rotation"]["checkbox"],
                                       "late_night": bool(p["properties"].get("Late Night Only", {}).get("checkbox"))}
        if not r.get("has_more"):
            return out
        cursor = r["next_cursor"]


QC_DIR = Path.home() / "wdbs-media" / "commercials" / "qc"
AUTO_SETS = ("2000VariousCommercials", "2001VariousCommercials", "2002variouscommercials", "2003variouscomercials",
             "2005variouscommercials", "2007VariousCommercials", "2009VariousCommercials")
POLICY = "Owner policy 2026-09-27: QC-clean 2000s spots auto-approve; flagged spots get human review."


def qc_records(sets=AUTO_SETS):
    out = []
    for p in sorted(QC_DIR.glob("*.json")):
        r = json.loads(p.read_text())
        if r["identifier"] in sets and r.get("air_master"):
            out.append(r)
    return out


def is_clean(r):
    q = r["qc"]
    return q["suggestion"] == "Approve" and not q["flags"] and r.get("spec_ok", False)


def auto_approve(apply):
    rows = rows_by_asset()
    todo = [(r, rows[r["id"]]) for r in qc_records() if is_clean(r) and rows.get(r["id"], {}).get("status") == "Needs Review"]
    print(f"{len(todo)} QC-clean 2000s spot(s) to auto-approve {'' if apply else '(dry run)'}")
    if not apply:
        return
    for r, row in todo:
        nr.call("PATCH", f"/pages/{row['page']}", {"properties": {
            "Status": {"select": {"name": "Approved"}},
            "Notes": nr.txt((row["notes"] + f" | Auto-approved {dt.date.today()}: QC clean, spec OK. {POLICY}")[:1990])}})
        time.sleep(0.35)
    print(f"auto-approved {len(todo)}")


def reviewed_names():
    r = sh(["ssh", "TV_HOST", "cut -f3 ~/.local/state/wdbs/commercial-review.tsv 2>/dev/null"])
    return set(r.stdout.split("\n"))


def stage_tv(apply, batch=60, sample=15):
    """Send the next review batch straight to the TV host's own disk (fast; skips the busy archive drive).
    Flagged 2000s spots first; the first batch also carries a random spot-check sample of auto-approved ones."""
    import random
    rows = rows_by_asset()
    done = reviewed_names()
    recs = [r for r in qc_records() if Path(r["air_master"]).name not in done]
    flagged = [r for r in recs if not is_clean(r) and rows.get(r["id"], {}).get("status") == "Needs Review"]
    spot = [r for r in recs if is_clean(r) and rows.get(r["id"], {}).get("status") == "Approved"]
    first = not (Path.home() / "wdbs-media" / "commercials" / ".spotcheck-sent").exists()
    pick = sorted(flagged, key=lambda r: r["air_master"])[:batch]
    if first and spot:
        random.seed(20260927)
        pick += random.sample(spot, min(sample, len(spot)))
    free = int(sh(["ssh", "TV_HOST", "df --output=avail -B1 ~ | tail -1"]).stdout.strip() or 0)
    size = sum(Path(r["air_master"]).stat().st_size for r in pick)
    print(f"{len(flagged)} flagged left, batch {len(pick)} ({size/1e9:.2f} GB), TV host free {free/1e9:.2f} GB")
    if size > free - 0.8e9:
        raise SystemExit("not enough free space on the TV host (keeping 0.8 GB headroom); lower --batch")
    if not apply or not pick:
        return
    sh(["ssh", "TV_HOST", "mkdir -p ~/wdbs-review-batch"])
    lst = Path("/tmp") / f"wdbs-batch-{os.getpid()}.txt"
    lst.write_text("\n".join(Path(r["air_master"]).name for r in pick) + "\n")
    r = subprocess.run(["rsync", "-a", f"--files-from={lst}", f"{AIR}/", "TV_HOST:wdbs-review-batch/"])
    lst.unlink()
    if first and spot:
        (Path.home() / "wdbs-media" / "commercials" / ".spotcheck-sent").write_text(dt.datetime.now().isoformat())
    print(f"staged {len(pick)} on the TV host (rsync rc={r.returncode})")


def clear_tv():
    sh(["ssh", "TV_HOST", "rm -rf ~/wdbs-review-batch"])
    print("cleared ~/wdbs-review-batch on the TV host")


def stage_review():
    REVIEW.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ionice", "-c", "3", "rsync", "-a", "--bwlimit=4000", "--include=*.mp4", "--exclude=*",
                        f"{AIR}/", f"{REVIEW}/"])
    print(f"staged {len(list(REVIEW.glob('*.mp4')))} file(s) in {REVIEW} (rsync rc={r.returncode})")


def sync(apply):
    local = Path.home() / "wdbs-media" / "commercials" / "commercial-review.tsv"
    r = sh(["scp", "-q", "-o", "BatchMode=yes", VERDICTS_REMOTE, str(local)])
    if r.returncode != 0:
        print("no verdicts on the TV host yet")
        return
    latest = {}
    for line in local.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            latest[parts[2]] = (parts[0], parts[1])
    rows = rows_by_asset()
    todo = []
    for fname, (ts, verdict) in latest.items():
        row = rows.get(asset_id(fname))
        if not row:
            print(f"  ? no Notion row for {fname}")
            continue
        want = "Approved" if verdict == "approve" else "Disabled"
        if row["status"] in ("Live",) and want == "Approved":
            continue
        if row["status"] != want:
            todo.append((row, want, ts, fname))
    print(f"{len(latest)} verdict(s); {len(todo)} Notion update(s) {'to apply' if apply else '(dry run)'}")
    for row, want, ts, fname in todo:
        print(f"  {row['status']} -> {want}: {fname}")
        if apply:
            note = (row["notes"] + f" | Reviewed on TV host {ts[:16]}: {want}.")[:1990]
            nr.call("PATCH", f"/pages/{row['page']}", {"properties": {
                "Status": {"select": {"name": want}}, "Notes": nr.txt(note)}})
            time.sleep(0.35)


LATE_NIGHT_PART = "latenight"   # FS42 day part in the TV host's confs/main_config.json (22:00-05:00)


def tv_has_late_night_part():
    """Only route into clips/latenight/ when FS42 defines that day part: otherwise the folder name is no
    hint at all and the spot would air at any hour."""
    r = sh(["ssh", "TV_HOST", "cat /path/to/FieldStation42/confs/main_config.json"])
    try:
        return LATE_NIGHT_PART in json.loads(r.stdout).get("day_parts", {})
    except ValueError:
        return False


def promote(apply):
    rows = rows_by_asset()
    # "Late Night Only" spots go to clips/latenight/ (FS42 day-part folder hint: airs 22:00-05:00 only),
    # and are held back entirely unless the TV host's FS42 config defines that day part.
    late_ok = tv_has_late_night_part()
    todo = [(aid, r) for aid, r in rows.items() if r["status"] == "Approved" and (late_ok or not r["late_night"])]
    n_held = sum(1 for aid, r in rows.items() if r["status"] == "Approved" and r["late_night"] and not late_ok)
    n_late = sum(1 for aid, r in todo if r["late_night"])
    print(f"{len(todo)} Approved spot(s) to promote {'' if apply else '(dry run)'} ({n_late} late-night only)"
          + (f"; {n_held} late-night spot(s) held: FS42 has no '{LATE_NIGHT_PART}' day part" if n_held else ""))
    if not todo or not apply:
        for aid, r in todo[:20]:
            print(f"  would copy {Path(r['path']).name} -> {LIVE / LATE_NIGHT_PART if r['late_night'] else LIVE}")
        return
    copied = []
    for aid, r in todo:
        src = Path(r["path"])
        dst = (LIVE / LATE_NIGHT_PART if r["late_night"] else LIVE) / f"WDBS__{src.name}"
        dst.parent.mkdir(exist_ok=True)
        if not src.exists():
            print(f"  ! missing air master {src}")
            continue
        if not dst.exists():
            tmp = dst.with_suffix(".part")
            shutil.copyfile(src, tmp)
            tmp.rename(dst)
        copied.append((aid, r, dst))
    print(f"copied {len(copied)} into {LIVE}; cataloging on the TV host (insert-only, content_type=commercial)...")
    # Files in clips/latenight/ are named explicitly (catalog sync v7 --file): the TV host's SMB client can keep
    # a stale listing of clips/ that misses a subfolder created on the app host's disk.
    extra = "".join(f" --file {shlex.quote(str(dst.relative_to(LIVE)))}" for aid, r, dst in copied
                    if dst.parent != LIVE)
    cat = sh(["ssh", "TV_HOST", "cd /path/to/FieldStation42 && nice -n 19 env/bin/python tools/wdbs_catalog_sync.py "
              "--station 'EXAMPLE SITCOMS' --extra-tag commercial --tag commercial --apply" + extra], timeout=3600)
    print("\n".join(l for l in cat.stdout.splitlines() if any(k in l for k in ("Inserted", "SAFE SYNC", "failures", "lost"))))
    if "SAFE SYNC PASSED" not in cat.stdout:
        print("catalog sync did not pass; Notion left at Approved")
        print(cat.stdout[-800:], cat.stderr[-800:])
        return
    q = ("select path, content_type, coalesce(hints, '') from catalog_entries "
         "where station='EXAMPLE SITCOMS' and tag='commercial'")
    got = sh(["ssh", "TV_HOST", f"cd /path/to/FieldStation42 && sqlite3 -separator '|' 'file:runtime/fs42_fluid.db?mode=ro' \"{q}\" "
              f"2>/dev/null || env/bin/python -c \"import sqlite3;c=sqlite3.connect('file:runtime/fs42_fluid.db?mode=ro',uri=True);"
              f"[print(a+'|'+b+'|'+h) for a,b,h in c.execute(\\\"{q}\\\")]\""]).stdout
    cataloged = {Path(l.split("|")[0]).name: l.split("|")[1:3] for l in got.splitlines() if l.count("|") >= 2}
    for aid, r, dst in copied:
        ctype, hints = cataloged.get(dst.name, [None, ""])
        if ctype != "commercial":
            print(f"  ! {dst.name} not cataloged as commercial ({ctype}); Notion unchanged")
            continue
        if r["late_night"] and LATE_NIGHT_PART not in hints:
            print(f"  ! {dst.name} is Late Night Only but has no '{LATE_NIGHT_PART}' hint in the catalog - "
                  "it could air at any hour; Notion left at Approved, check it")
            continue
        note = (r["notes"] + f" | Promoted {dt.date.today()}: live in Channel 10 commercial pool.")[:1990]
        nr.call("PATCH", f"/pages/{r['page']}", {"properties": {
            "Status": {"select": {"name": "Live"}}, "In Rotation": {"checkbox": True},
            "Filename / Path": nr.txt(f"{TV_LIVE}/{dst.name}"), "Notes": nr.txt(note)}})
        time.sleep(0.35)
    print("promotion complete; new spots enter breaks as new schedule blocks are built (04:30 or replan)")


if __name__ == "__main__":
    cmd, apply = (sys.argv[1] if len(sys.argv) > 1 else "sync"), "--apply" in sys.argv
    {"stage-review": lambda: stage_review(), "sync": lambda: sync(apply), "promote": lambda: promote(apply),
     "auto-approve": lambda: auto_approve(apply), "stage-tv": lambda: stage_tv(apply),
     "clear-tv": lambda: clear_tv()}[cmd]()
