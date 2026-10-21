#!/usr/bin/env python3
"""
WDBS safe incremental FieldStation42 catalog sync v7.

v7: --file REL (repeatable) adds files by path relative to the tag dir, with the same folder
    hints a scan would give. For files in a folder the TV host's SMB client still lists from a stale
    directory cache (a new subfolder created on the server's disk, not through Samba).

v6: --apply takes the shared WDBS FS42 write lock (waits up to --lock-wait s) so it
    never interleaves with a replan / overnight / weather write and its restore.

v5: files in the station's commercial_dir / bump_dir are inserted with content_type
'commercial' / 'bump' (v4 inserted everything as 'feature').

Safety rules:
- never rebuild the FieldStation42 catalog
- never UPDATE/REPLACE/DELETE existing catalog rows
- never touch liquid_blocks
- dry-run performs zero ffprobe calls
- only genuinely new files are probed
- per-file probe timeout prevents one bad/network file from hanging the worker
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fs42.catalog import ShowCatalog
import fs42.catalog as catalog_mod
from fs42.media_processor import MediaProcessor
from fs42.station_manager import StationManager

LOG = logging.getLogger("WDBS-CATALOG-SYNC")


class ProbeTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise ProbeTimeout("ffprobe timed out")


def configured_feature_tags(config: dict) -> list[str]:
    tags = {}
    bump_overrides = {}
    commercial_overrides = {}
    start_bumps = {}
    end_bumps = {}

    days = getattr(
        catalog_mod,
        "DAYS",
        ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    )

    for day in days:
        slots = config.get(day, {})
        if not isinstance(slots, dict):
            continue
        for slot in slots.values():
            if isinstance(slot, dict):
                ShowCatalog._harvest_slot(
                    slot, tags, bump_overrides, commercial_overrides, start_bumps, end_bumps
                )

    for override_slots in (config.get("date_overrides", {}) or {}).values():
        if not isinstance(override_slots, dict):
            continue
        for slot in override_slots.values():
            if isinstance(slot, dict):
                ShowCatalog._harvest_slot(
                    slot, tags, bump_overrides, commercial_overrides, start_bumps, end_bumps
                )

    for week_schedule in (config.get("week_overrides", {}) or {}).values():
        if not isinstance(week_schedule, dict):
            continue
        for day in days:
            slots = week_schedule.get(day, {})
            if not isinstance(slots, dict):
                continue
            for slot in slots.values():
                if isinstance(slot, dict):
                    ShowCatalog._harvest_slot(
                        slot, tags, bump_overrides, commercial_overrides, start_bumps, end_bumps
                    )

    fallback = config.get("fallback_tag")
    if fallback:
        tags[fallback] = True

    for clip_tag in (config.get("clip_shows", {}) or {}):
        tags[clip_tag] = True

    return sorted(tags.keys())


def db_path() -> str:
    return StationManager().server_conf["db_path"]


def existing_catalog(conn: sqlite3.Connection, station: str):
    rows = conn.execute(
        """
        SELECT id, path, realpath, tag, count
        FROM catalog_entries
        WHERE station = ?
        """,
        (station,),
    ).fetchall()

    by_key = {(tag, path): row_id for row_id, path, realpath, tag, count in rows}
    by_realkey = {
        (tag, realpath): row_id
        for row_id, path, realpath, tag, count in rows
        if realpath
    }
    ids = {row_id for row_id, path, realpath, tag, count in rows}
    return rows, by_key, by_realkey, ids


def scheduled_content_ids(conn: sqlite3.Connection, station: str) -> set[int]:
    refs = set()
    rows = conn.execute(
        "SELECT content_json FROM liquid_blocks WHERE station = ?",
        (station,),
    ).fetchall()

    for (raw,) in rows:
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except Exception:
            continue

        values = value if isinstance(value, list) else [value]
        for item in values:
            try:
                refs.add(int(item))
            except Exception:
                pass

    return refs


_write_lock_fh = None


def hold_write_lock(wait_s: int) -> None:
    """Take the shared WDBS FS42 write lock (same file as wdbs.replan.write_lock) until exit.
    Skipped when a parent WDBS process (the overnight job) already holds it for us."""
    global _write_lock_fh
    state_db = Path(os.environ.get("WDBS_STATE_DB", "~/.local/state/wdbs/wdbs.db")).expanduser()
    lock_path = state_db.parent / "fs42-write.lock"
    if os.environ.get("WDBS_FS42_LOCK_HELD") == str(lock_path):
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SystemExit(f"Another WDBS write operation holds {lock_path}; nothing written.")
            time.sleep(2)
    _write_lock_fh = fh


def create_backup(dbfile: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{dbfile}.wdbs-catalog-sync-{stamp}.bak"
    src = sqlite3.connect(dbfile)
    dst = sqlite3.connect(backup)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup


def serialize_hints(entry):
    hints = []
    for hint in (entry.hints or []):
        hints.append(json.dumps(hint.toJSON()))
    return json.dumps(hints) if hints else None


def enumerate_tag_files(config: dict, tag: str):
    media_filter = config.get("media_filter", "video")
    content_dir = config["content_dir"]
    tag_dir = tag if os.path.isabs(tag) else os.path.join(content_dir, tag)

    if not os.path.isdir(tag_dir):
        return tag_dir, []

    found = []

    for path in MediaProcessor._find_media(tag_dir, media_filter):
        found.append((path, []))

    root = os.path.abspath(tag_dir)
    all_files = [
        f for f in MediaProcessor._rfind_media(tag_dir, media_filter)
        if os.path.dirname(os.path.abspath(f)) != root
    ]

    files_by_dir = defaultdict(list)
    for f in all_files:
        files_by_dir[os.path.dirname(f)].append(f)

    for media_dir, file_list in files_by_dir.items():
        hints = []
        rel_path = os.path.relpath(media_dir, tag_dir)
        if rel_path != ".":
            for part in rel_path.split(os.sep):
                hints += MediaProcessor._process_hints(part, tag, False)

        for path in file_list:
            found.append((path, hints))

    found.sort(key=lambda item: item[0])
    return tag_dir, found


def explicit_files(config: dict, tag: str, rel_paths):
    """Files named relative to the tag dir, with the folder hints a scan would derive."""
    content_dir = config["content_dir"]
    tag_dir = tag if os.path.isabs(tag) else os.path.join(content_dir, tag)
    out = []
    for rel in rel_paths or []:
        rel = os.path.normpath(rel)
        if os.path.isabs(rel) or rel.startswith(".."):
            raise SystemExit(f"--file must be relative to the tag dir and inside it: {rel}")
        path = os.path.join(tag_dir, rel)
        if not os.path.isfile(path):
            raise SystemExit(f"--file not found: {path}")
        hints = []
        for part in [x for x in os.path.dirname(rel).split(os.sep) if x]:
            hints += MediaProcessor._process_hints(part, tag, False)
        out.append((path, hints))
    return out


def is_existing(tag, path, by_key, by_realkey):
    if (tag, path) in by_key:
        return True
    realpath = os.path.realpath(path)
    return bool(realpath and (tag, realpath) in by_realkey)


def insert_entry(conn: sqlite3.Connection, station: str, entry) -> bool:
    realpath = entry.realpath or os.path.realpath(entry.path)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO catalog_entries
            (station, path, realpath, title, duration, tag, count, hints,
             content_type, media_type, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            station,
            entry.path,
            realpath,
            entry.title,
            float(entry.duration),
            entry.tag,
            int(entry.count or 0),
            serialize_hints(entry),
            getattr(entry, "content_type", "feature"),
            getattr(entry, "media_type", "video"),
        ),
    )
    return cur.rowcount == 1


def content_type_for_tag(config: dict, tag: str) -> str:
    """WDBS: the station's commercial/bump directories hold commercials/bumps, not features.
    (v4 inserted everything as 'feature', which would have put ads into episode pools.)"""
    if tag == config.get("commercial_dir"):
        return "commercial"
    if tag == config.get("bump_dir"):
        return "bump"
    return "feature"


def process_with_timeout(path, tag, hints, timeout_s, content_type="feature"):
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout_s)
    try:
        return MediaProcessor.process_one(
            path, tag, hints=hints, fluid=None, content_type=content_type
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--station", default=os.getenv("WDBS_FS42_STATION", "EXAMPLE SITCOMS"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--tag", action="append")
    parser.add_argument("--extra-tag", action="append", default=[])
    parser.add_argument("--max-new", type=int, default=None)
    parser.add_argument("--probe-timeout", type=int, default=45)
    parser.add_argument("--lock-wait", type=int, default=900, help="seconds to wait for the WDBS write lock")
    parser.add_argument("--file", action="append", default=[],
                        help="file relative to the (single) --tag dir; added even if a listing misses it")
    parser.add_argument("--list-tags", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mgr = StationManager()
    config = mgr.station_by_name(args.station)
    if not config:
        raise SystemExit(f"Station not found: {args.station!r}")
    if config.get("network_type") != "standard":
        raise SystemExit("This WDBS sync currently supports standard stations only.")

    configured_tags = configured_feature_tags(config)
    extra_tags = sorted(set(args.extra_tag or []))
    tags = list(dict.fromkeys(configured_tags + extra_tags))

    if args.tag:
        requested = set(args.tag)
        tags = [t for t in tags if t in requested]

    print(f"Station: {args.station}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (NO FFPROBE)'}")
    print(f"Scan tags: {', '.join(tags) if tags else '(none)'}")

    if args.list_tags:
        return 0

    dbfile = db_path()
    conn = sqlite3.connect(dbfile, timeout=30)

    try:
        before_rows, by_key, by_realkey, before_ids = existing_catalog(conn, args.station)
        schedule_refs = scheduled_content_ids(conn, args.station)
        pre_orphans = sorted(schedule_refs.difference(before_ids))

        print(f"Existing catalog entries: {len(before_rows)}")
        print(f"Schedule-referenced catalog IDs: {len(schedule_refs)}")
        print(f"Pre-existing orphan schedule IDs: {len(pre_orphans)}")

        new_files = []
        seen = set()

        for tag in tags:
            print(f"\nScanning tag: {tag}", flush=True)
            tag_dir, found = enumerate_tag_files(config, tag)
            if args.file:
                if len(tags) != 1:
                    raise SystemExit("--file needs exactly one --tag")
                listed = {p for p, _ in found}
                found += [(p, h) for p, h in explicit_files(config, tag, args.file) if p not in listed]
            if not os.path.isdir(tag_dir):
                print(f"  MISSING DIRECTORY: {tag_dir}", flush=True)
                continue

            tag_new = 0
            for path, hints in found:
                key = (tag, path)
                if key in seen:
                    continue
                seen.add(key)
                if not is_existing(tag, path, by_key, by_realkey):
                    new_files.append((tag, path, hints))
                    tag_new += 1

            print(f"  Files discovered without probing: {len(found)}", flush=True)
            print(f"  New files: {tag_new}", flush=True)

        print(f"\nNew files requiring catalog entries: {len(new_files)}")

        if not args.apply:
            print("DRY RUN complete. No ffprobe calls or database writes were made.")
            return 0

        if not new_files:
            print("Nothing new to process.")
            return 0

        if args.max_new is not None:
            if args.max_new < 1:
                raise SystemExit("--max-new must be at least 1")
            new_files = new_files[: args.max_new]

        hold_write_lock(args.lock_wait)
        backup = create_backup(dbfile)
        print(f"SQLite backup created: {backup}")

        inserted = 0
        failed = []

        for i, (tag, path, hints) in enumerate(new_files, 1):
            print(f"[{i}/{len(new_files)}] {path}", flush=True)
            try:
                entry = process_with_timeout(path, tag, hints, args.probe_timeout,
                                             content_type_for_tag(config, tag))
            except ProbeTimeout:
                print(f"  TIMEOUT after {args.probe_timeout}s", flush=True)
                failed.append({"path": path, "reason": "timeout"})
                continue
            except Exception as exc:
                print(f"  FAILED: {exc}", flush=True)
                failed.append({"path": path, "reason": str(exc)})
                continue

            if not entry or entry.duration < 1:
                print("  FAILED: no valid catalog entry/duration", flush=True)
                failed.append({"path": path, "reason": "invalid entry"})
                continue

            conn.execute("BEGIN IMMEDIATE")
            try:
                if insert_entry(conn, args.station, entry):
                    inserted += 1
                    print(f"  INSERTED: {entry.title}", flush=True)
                else:
                    print("  SKIPPED: row already exists", flush=True)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        _after_rows, _bk, _br, after_ids = existing_catalog(conn, args.station)
        post_orphans = sorted(schedule_refs.difference(after_ids))
        lost_old_ids = sorted(before_ids.difference(after_ids))

        print("\n=== Safety check ===")
        print(f"Inserted: {inserted}")
        print(f"Probe/process failures: {len(failed)}")
        print(f"Existing IDs lost: {len(lost_old_ids)}")
        print(f"Schedule orphan IDs before: {len(pre_orphans)}")
        print(f"Schedule orphan IDs after: {len(post_orphans)}")

        if lost_old_ids:
            raise RuntimeError(f"Old catalog IDs disappeared. Backup: {backup}")
        if set(post_orphans) != set(pre_orphans):
            raise RuntimeError(f"Schedule-reference integrity changed. Backup: {backup}")

        print("SAFE SYNC PASSED: existing catalog IDs and schedule references were preserved.")
        return 2 if failed else 0

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
