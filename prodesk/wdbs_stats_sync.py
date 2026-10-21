#!/usr/bin/env python3
"""app host: write Channel 10 playout stats from the TV host into the Notion WDBS Programming rows.

Runs `wdbs --json history --channel 10` on the TV host over the existing `ssh TV_HOST` key (the recorder's
qualified airings + the FS42 schedule), then PATCHes each Programming row whose FS42 Tag matches:
Last Aired, First WDBS Air, Airings 7d / 30d, WDBS Airings Total, Unique Episodes Aired, Unplayed Episodes,
Last Episode Aired, Next Episode, Next Scheduled. Only changed properties are sent, so a quiet run makes no
writes. Owner inputs (Cooldown Until, Rotation Weight, Dayparts, ...) are never touched. Stdlib only.
Usage: wdbs_stats_sync.py [--dry-run]      (wdbs-stats-sync.timer, every 15 min)
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location("wdbs_ratings_sync", Path(__file__).with_name("wdbs_ratings_sync.py"))
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)  # Notion call/query helpers and token handling, shared with the ratings sync

CHANNEL = 10
TZ = ZoneInfo("America/New_York")   # the TV host records local wall-clock times
DATES = {"Last Aired": "last_aired", "First WDBS Air": "first_aired", "Next Scheduled": "next_scheduled_at"}
NUMBERS = {"Airings 7d": "airings_7d", "Airings 30d": "airings_30d", "WDBS Airings Total": "airings_total",
           "Unique Episodes Aired": "unique_episodes_aired", "Unplayed Episodes": "unplayed_episodes"}
TEXTS = {"Last Episode Aired": "last_episode_aired", "Next Episode": "next_episode"}
# First WDBS Air was set by hand for the first shows (9/25-9/27, before the recorder existed): fill it only
# when blank. Last Aired / Last Episode Aired are never cleared just because the TV host has no qualified airing.
FILL_ONLY = {"First WDBS Air"}
NEVER_CLEAR = {"First WDBS Air", "Last Aired", "Last Episode Aired"}


def log(msg: str) -> None:
    print(f"wdbs-stats-sync: {msg}", flush=True)


def fetch_history() -> list[dict]:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "TV_HOST",
                        f"~/.local/bin/wdbs --json history --channel {CHANNEL}"],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise rs.NotionError(f"wdbs history on the TV host failed: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout)["programs"]


def to_minute(local: str | None) -> str | None:
    """'2026-09-29 23:31:54' (TV host local) -> '2026-09-29T23:31:00-04:00' (Notion keeps minutes)."""
    if not local:
        return None
    t = dt.datetime.fromisoformat(local).replace(second=0, microsecond=0, tzinfo=TZ)
    return t.isoformat()


def _current(prop: dict):
    t = prop.get("type")
    if t == "date":
        start = (prop.get("date") or {}).get("start")
        if not start:
            return None
        d = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        return d.astimezone(TZ).replace(second=0, microsecond=0).isoformat() if d.tzinfo else start
    if t == "number":
        return prop.get("number")
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", [])) or None
    return None


def wanted(h: dict) -> dict:
    """Notion property name -> (type, value) this program should have."""
    out = {}
    for name, key in DATES.items():
        out[name] = ("date", to_minute(h.get(key)))
    for name, key in NUMBERS.items():
        v = h.get(key)
        if name == "Unplayed Episodes" and not h.get("episodes_in_library"):
            continue                       # library unknown on the TV host: don't claim 0 unplayed
        out[name] = ("number", int(v) if v is not None else None)
    for name, key in TEXTS.items():
        out[name] = ("rich_text", (h.get(key) or None) and str(h.get(key))[:rs.CHUNK])
    return out


def patch_body(page: dict, want: dict) -> dict:
    props = page.get("properties", {})
    body = {}
    for name, (typ, value) in want.items():
        if name not in props:
            continue
        have = _current(props[name])
        if have == value or (value is None and name in NEVER_CLEAR) or (name in FILL_ONLY and have is not None):
            continue
        if typ == "date":
            body[name] = {"date": {"start": value} if value else None}
        elif typ == "number":
            body[name] = {"number": value}
        else:
            body[name] = {"rich_text": [{"type": "text", "text": {"content": value}}] if value else []}
    return body


def sync(history: list[dict], call, dry_run: bool = False) -> int:
    pages = rs._query(call, rs.DS_PROGRAMMING, {"filter": {"property": "FS42 Tag", "rich_text": {"is_not_empty": True}}})
    by_tag = {}
    for p in pages:
        props = p.get("properties", {})
        tag = _current(props.get("FS42 Tag", {}))
        if tag and (props.get("Channel", {}).get("number") in (None, CHANNEL)):
            by_tag[tag] = p
    changed = 0
    for h in history:
        page = by_tag.get(h["tag"])
        if page is None:
            log(f"no Programming row with FS42 Tag {h['tag']!r}; skipped")
            continue
        body = patch_body(page, wanted(h))
        if not body:
            continue
        changed += 1
        log(f"{'would update' if dry_run else 'update'} {h.get('program') or h['tag']}: {', '.join(sorted(body))}")
        if not dry_run:
            call("PATCH", f"/pages/{page['id']}", {"properties": body})
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        n = sync(fetch_history(), rs.make_call(), dry_run=a.dry_run)
    except (rs.NotionError, OSError, subprocess.SubprocessError, ValueError) as exc:
        log(f"failed: {exc}")
        return 1
    log(f"{n} row(s) {'would change' if a.dry_run else 'updated'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
