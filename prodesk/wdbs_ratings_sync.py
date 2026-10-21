#!/usr/bin/env python3
"""app host: copy new Remote v2 ratings from the TV host into the Notion WDBS Ratings Log.

Reads ~/.local/state/wdbs/ratings.jsonl on the TV host over the existing `ssh TV_HOST` key, from the last synced
line (state: ~/.local/state/wdbs/ratings-sync.json). Idempotent by Rating ID. A Notion failure stops the run
without advancing past the last row created, so the next run retries. Stdlib only.

Usage: wdbs_ratings_sync.py [--dry-run]      (wdbs-ratings-sync.timer, every 2 min)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
DS_RATINGS = os.environ.get("WDBS_RATINGS_DS", "00000000-0000-4000-8000-000000000002")
DS_PROGRAMMING = "00000000-0000-4000-8000-000000000003"
DS_COMMERCIALS = "00000000-0000-4000-8000-000000000001"
REMOTE = ".local/state/wdbs/ratings.jsonl"
STATE = Path.home() / ".local/state/wdbs/ratings-sync.json"
KINDS = {"episode": "Episode", "commercial": "Commercial", "weather": "Weather", "bump": "Bumper/ID",
         "guide": "Guide", "today": "WDBS Today", "web": "Web", "standby": "Standby"}
CHUNK = 1900


class NotionError(Exception):
    pass


def log(msg: str) -> None:
    print(f"wdbs-ratings-sync: {msg}", flush=True)


def token() -> str:
    for line in (Path.home() / ".config" / "wdbs-notion-sonarr.env").read_text().splitlines():
        if line.startswith("NOTION_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise NotionError("NOTION_TOKEN not found")


def make_call():
    tok = token()

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {tok}", "Notion-Version": VERSION,
                                              "Content-Type": "application/json"})
        for attempt in range(5):
            try:
                return json.load(urllib.request.urlopen(req, timeout=60))
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503):
                    time.sleep(2 + attempt * 2)
                    continue
                raise NotionError(f"{e.code}: {e.read()[:300]!r}")
            except (urllib.error.URLError, TimeoutError) as e:
                raise NotionError(str(e))
        raise NotionError("rate-limited repeatedly")
    return call


def fetch_lines(start: int) -> list[str]:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "TV_HOST",
                        f"tail -n +{start + 1} {REMOTE} 2>/dev/null || true"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise NotionError(f"ssh TV_HOST failed: {r.stderr.strip()[:200]}")
    # WDBS: split on "\n" only - str.splitlines() also breaks on U+2028/U+2029/etc, which
    # append_jsonl (ensure_ascii=False) can write raw inside a JSON string, splitting one
    # physical line into two and shifting our saved line count out of step with `tail -n +N`.
    lines = r.stdout.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _txt(s) -> dict:
    return {"rich_text": [{"text": {"content": str(s)[:CHUNK]}}]} if s else {"rich_text": []}


def title(rec: dict) -> str:
    t = f"{'👍' if rec['thumb'] == 'up' else '👎'} {rec.get('label') or 'unknown'}"
    return f"{t} · {rec['reason']}" if rec.get("reason") else t


def properties(rec: dict, show_id: str | None, spot_id: str | None) -> dict:
    ep = " ".join(x for x in (rec.get("episode"), rec.get("episode_title")) if x)
    p = {"Rating": {"title": [{"text": {"content": title(rec)[:CHUNK]}}]},
         "Thumb": {"select": {"name": "👍" if rec["thumb"] == "up" else "👎"}},
         "When": {"date": {"start": rec["at"]}},
         "Channel": {"number": rec.get("channel")},
         "Show": {"relation": [{"id": show_id}] if show_id else []},
         "Commercial": {"relation": [{"id": spot_id}] if spot_id else []},
         "Episode": _txt(ep), "File": _txt(rec.get("file")), "Position": _txt(rec.get("position")),
         "Review": {"select": {"name": "New"}}, "Rating ID": _txt(rec["id"])}
    if rec.get("reason"):
        p["Reason"] = {"select": {"name": rec["reason"]}}
    if rec.get("kind") in KINDS:
        p["Kind"] = {"select": {"name": KINDS[rec["kind"]]}}
    if rec.get("daypart"):
        p["Daypart"] = {"select": {"name": rec["daypart"]}}
    return p


def children(rec: dict, notes: list[str]) -> list[dict]:
    snap = json.dumps(rec, indent=1, ensure_ascii=False)
    blocks = [{"object": "block", "type": "code", "code": {"language": "json", "rich_text": [
        {"type": "text", "text": {"content": snap[i:i + CHUNK]}} for i in range(0, len(snap), CHUNK)][:100]}}]
    for n in notes:
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": n}}]}})
    return blocks


def _query(call, ds: str, body: dict) -> list[dict]:
    out, cursor = [], None
    while True:
        r = call("POST", f"/data_sources/{ds}/query", {**body, "page_size": 100, **({"start_cursor": cursor} if cursor else {})})
        out += r["results"]
        if not r.get("has_more"):
            return out
        cursor = r["next_cursor"]


def show_index(call) -> dict[str, str]:
    idx = {}
    for p in _query(call, DS_PROGRAMMING, {"filter": {"property": "FS42 Tag", "rich_text": {"is_not_empty": True}}}):
        tag = "".join(t.get("plain_text", "") for t in p["properties"]["FS42 Tag"]["rich_text"]).strip()
        if tag:
            idx[tag] = p["id"]
    return idx


def find_spot(call, filename: str) -> str | None:
    hits = _query(call, DS_COMMERCIALS, {"filter": {"property": "Filename / Path", "rich_text": {"contains": filename}}})
    return hits[0]["id"] if hits else None


def exists(call, rid: str) -> bool:
    return bool(_query(call, DS_RATINGS, {"filter": {"property": "Rating ID", "rich_text": {"equals": rid}}}))


def _save(state_path: Path, line: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps({"line": line, "at": time.time()}))
    os.replace(tmp, state_path)


def sync(fetch, call, state_path: Path, dry_run: bool = False) -> int:
    start = json.loads(state_path.read_text())["line"] if state_path.exists() else 0
    lines = fetch(start)
    if not lines:
        return 0
    shows, created = None, 0
    for i, line in enumerate(lines, start=start + 1):
        try:
            rec = json.loads(line)
            rec["id"], rec["thumb"], rec["at"]
        except (ValueError, KeyError, TypeError):
            log(f"line {i}: not a rating, skipped")
            if not dry_run:
                _save(state_path, i)
            continue
        if exists(call, rec["id"]):
            if not dry_run:
                _save(state_path, i)
            continue
        notes, show_id, spot_id = [], None, None
        if rec.get("show_tag"):
            shows = shows if shows is not None else show_index(call)
            show_id = shows.get(rec["show_tag"])
            if not show_id:
                notes.append(f"No WDBS Programming row has FS42 Tag '{rec['show_tag']}'.")
        if rec.get("commercial_file"):
            spot_id = find_spot(call, rec["commercial_file"])
            if not spot_id:
                notes.append(f"No WDBS Commercial Library row matches '{rec['commercial_file']}'.")
        body = {"parent": {"type": "data_source_id", "data_source_id": DS_RATINGS},
                "properties": properties(rec, show_id, spot_id), "children": children(rec, notes)}
        if dry_run:
            log(f"would create: {title(rec)}")
        else:
            call("POST", "/pages", body)
            _save(state_path, i)
        created += 1
    return created


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        n = sync(fetch_lines, make_call(), STATE, dry_run=a.dry_run)
    except NotionError as e:
        log(f"stopped: {e}")
        return 1
    if n:
        log(f"{'would create' if a.dry_run else 'created'} {n} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
