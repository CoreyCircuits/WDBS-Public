#!/usr/bin/env python3
"""Create WDBS Commercial Library rows (Notion) from the ingest manifest.

Every row is Status = "Needs Review", In Rotation = unchecked: automation flags,
the owner approves . Idempotent: rows are matched by Source text,
so re-running never duplicates. Uses the existing WDBS Notion integration token
(never printed).

    commercial_notion_rows.py dry-run
    commercial_notion_rows.py apply
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DS = "00000000-0000-4000-8000-000000000001"
MANIFEST = Path.home() / "wdbs-media" / "commercials" / "manifest.json"
API = "https://api.notion.com/v1"


def token():
    for line in (Path.home() / ".config" / "wdbs-notion-sonarr.env").read_text().splitlines():
        if line.startswith("NOTION_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("NOTION_TOKEN not found")


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2025-09-03",
                                          "Content-Type": "application/json"})
    for attempt in range(5):
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503):
                time.sleep(2 + attempt * 2)
                continue
            raise SystemExit(f"Notion {e.code}: {e.read()[:300]}")
    raise SystemExit("Notion rate-limited repeatedly")


def existing_sources():
    out, cursor = set(), None
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        r = call("POST", f"/data_sources/{DS}/query", body)
        for p in r["results"]:
            src = p["properties"].get("Source", {}).get("rich_text", [])
            out.add("".join(t.get("plain_text", "") for t in src))
        if not r.get("has_more"):
            return out
        cursor = r["next_cursor"]


def txt(s):
    return {"rich_text": [{"text": {"content": s[:1990]}}]} if s else {"rich_text": []}


def row(m):
    title = m["title"]
    if m.get("year") and title.startswith(f"{m['year']} - "):
        title = title[len(f"{m['year']} - "):]
    nick = "Collectionof90sCommercials" in m["source"]
    notes = [f"QC suggestion: {m['suggestion']}."]
    if m["flags"]:
        notes.append("Flags: " + "; ".join(m["flags"]) + ".")
    notes.append(f"Source {m['source_res']}, {m['loudness_i']} LUFS / TP {m['true_peak']} dBFS -> air master "
                 f"{m.get('air_loudness_i')} LUFS (house target -24 LUFS / -2 dBTP).")
    if nick:
        notes.append("Source collection says 'mostly aired on Nickelodeon'; brand/title not labelled - name on preview.")
    notes.append(f"Duration class {m['duration_class']}s. Imported {dt.date.today()} by WDBS ingest; pool is a suggestion.")
    props = {
        "Commercial": {"title": [{"text": {"content": title[:200]}}]},
        "Status": {"select": {"name": "Needs Review"}},
        "In Rotation": {"checkbox": False},
        "Era": {"select": {"name": m["era"]}},
        "Duration Sec": {"number": round(m["duration"], 2)},
        "Channel Pools": {"multi_select": [{"name": "Sitcoms"}]},
        "Source": txt(m["source"]),
        "Filename / Path": txt(m.get("air_master") or ""),
        "Notes": txt(" ".join(notes)),
    }
    if m.get("year"):
        props["Year"] = {"number": m["year"]}
    if m.get("brand"):
        props["Brand"] = txt(m["brand"])
    if m.get("category_guess"):
        props["Category"] = {"select": {"name": m["category_guess"]}}
    if nick:
        props["Audience"] = {"select": {"name": "Kids"}}
    return {"parent": {"type": "data_source_id", "data_source_id": DS}, "properties": props}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry-run"
    rows = json.loads(MANIFEST.read_text())
    have = existing_sources()
    todo = [m for m in rows if m["source"] not in have and m["suggestion"] != "Reject"]
    missing_master = [m for m in todo if not m.get("air_master")]
    print(f"manifest {len(rows)} | already in Notion {len(rows) - len(todo)} | to create {len(todo)} | "
          f"without air master {len(missing_master)}")
    if mode == "dry-run":
        print(json.dumps(row(todo[0])["properties"], indent=1)[:1500] if todo else "nothing to do")
        return 0
    made = 0
    for m in todo:
        call("POST", "/pages", row(m))
        made += 1
        if made % 25 == 0:
            print(f"  created {made}/{len(todo)}", flush=True)
        time.sleep(0.4)
    print(f"created {made} row(s)")
    return 0


TOKEN = token()
if __name__ == "__main__":
    sys.exit(main())
