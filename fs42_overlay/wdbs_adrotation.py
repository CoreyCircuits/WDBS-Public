"""WDBS ad rotation for FieldStation42 schedule builds.

FS42 already picks the lowest-count candidate and bumps the in-memory count after
each pick, but commercial counts start every build at the stored value (always 0 -
only features are persisted), so each day's rotation restarts at random, and the
end-of-break remainder fill (where only short spots fit) reuses the same spot.

This module (installed into fs42/ by wdbs.fs42_patch "ad-rotation"):
  * seed(): at the start of a build, set each commercial's in-memory count to how
    often it aired in the last SEED_WINDOW of schedule *before the point the build
    starts from* (the current schedule end: append, panic, or a replan cutoff).
    The new day continues the rotation instead of re-airing the spots that just ran.
    A short window keeps new spots (count 0) from monopolising breaks; they get a
    small head start, then rotate evenly.
  * block-level no-repeat: spots already used in the current block are passed
    over whenever any other spot fits (a repeat is allowed only if nothing else fits).
  * dayparted spots (FS42 folder hints such as commercial/latenight/) are levelled up to the
    rotation frontier (least-used unrestricted spot of no greater length), so they rotate in instead of taking every slot of
    their first allowed hour.
Nothing is written to the database; only in-memory counts during a build change.
Set WDBS_ADROTATION=0 to disable.
"""

import collections
import datetime
import json
import os
import sqlite3

ENABLED = os.environ.get("WDBS_ADROTATION", "1") != "0"
SEED_WINDOW = datetime.timedelta(hours=3)
AVOID = 10_000


def _commercial_entries(catalog):
    for entries in catalog.clip_index.values():
        for e in entries or []:
            if getattr(e, "content_type", None) == "commercial":
                yield e


def seed(catalog, conf):
    if not ENABLED:
        return
    try:
        from fs42.station_manager import StationManager
        db = StationManager().server_conf["db_path"]
        used = collections.Counter()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            end = conn.execute("SELECT max(end_time) FROM liquid_blocks WHERE station = ?",
                               (conf["network_name"],)).fetchone()[0]
            if not end:
                return
            since = (datetime.datetime.fromisoformat(end) - SEED_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
            for (plan,) in conn.execute("SELECT plan_json FROM liquid_blocks WHERE station = ? AND end_time > ?",
                                        (conf["network_name"], since)):
                for p in json.loads(plan or "[]"):
                    if p.get("content_type") == "commercial":
                        used[p.get("path")] += 1
        finally:
            conn.close()
        for e in _commercial_entries(catalog):
            e.count = used.get(e.path, 0)
    except Exception:
        pass  # never block a schedule build over rotation bookkeeping


def begin_block(catalog):
    catalog._wdbs_block_ads = set()


def _level_restricted(entries):
    """Time-restricted spots (FS42 folder hints, e.g. commercial/latenight/) are filtered out of every
    break outside their hours, so their count stays low and they would swamp the first breaks they are
    allowed in. Lift each to the rotation frontier: the lowest count among unrestricted spots that could
    fill the same slot (no longer than it). Long spots that rarely fit sit near zero and are ignored."""
    free = [(e.duration, e.count) for e in entries if not getattr(e, "hints", None)]
    if not free:
        return
    for e in entries:
        if getattr(e, "hints", None):
            fits = [c for d, c in free if d <= e.duration + 0.5]
            if fits and e.count < min(fits):
                e.count = min(fits)


def before_pick(catalog, tag):
    """Temporarily push spots already used in this block to the back of the queue."""
    if not ENABLED:
        return []
    _level_restricted(catalog.clip_index.get(tag, []))
    used = getattr(catalog, "_wdbs_block_ads", None)
    if not used:
        return []
    bumped = [e for e in catalog.clip_index.get(tag, []) if e.path in used]
    for e in bumped:
        e.count += AVOID
    return bumped


def after_pick(catalog, bumped, result):
    for e in bumped:
        e.count -= AVOID
    if ENABLED and result is not None and hasattr(catalog, "_wdbs_block_ads"):
        catalog._wdbs_block_ads.add(result.path)
