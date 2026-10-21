"""WDBS Stream library - pure logic for the Library tab and Add/Remove from Library (docs/WDBS-STREAM.md §2).

No network, no processes. The owner's Stremio library syncs to every Stremio device, so every write here is exactly
what Stremio itself does (stremio-core AddToLibrary / RemoveFromLibrary):

  in library  = removed false and temp false
  add         = removed false, temp false, _mtime now (a new item is created the way Stremio creates one)
  remove      = removed true (state and temp kept), _mtime now

Unknown fields are always kept and inputs are never mutated.
"""

from __future__ import annotations

from datetime import datetime

import stream_core as core

RECENT_MAX = 20
STARTED_AT = 0.01           # below this much of a video, a resume point is noise (Stremio leaves timeOffset 1)


# ------------------------------------------------------------------ item transforms
def in_library(item: dict | None) -> bool:
    return bool(item) and not item.get("removed") and not item.get("temp")


def set_in_library(item: dict | None, meta: dict, add: bool, now: datetime) -> dict | None:
    """The library item after Add (add=True) or Remove (add=False).

    item: the account's current item for meta["id"] or None. meta: {id, type, name, poster} of the title.
    Returns the item to write, or None when nothing needs writing (already in that state, or removing an item the
    account never had)."""
    if item is None:
        if not add:
            return None
        new = core.new_library_item(meta, now)
        new.update(removed=False, temp=False)
        return new
    if add == in_library(item):
        return None
    out = {**item, "state": {**(item.get("state") or {})}}
    if item.get("behaviorHints") is not None:
        out["behaviorHints"] = {**item["behaviorHints"]}
    if add:
        out["removed"] = False
        out["temp"] = False
        for key in ("name", "poster"):
            if not out.get(key) and meta.get(key):
                out[key] = meta[key]
        if not out.get("type") and meta.get("type"):
            out["type"] = meta["type"]
    else:
        out["removed"] = True
    out["_mtime"] = core._iso(now)
    return out


# ------------------------------------------------------------------ Library tab
def _progress(item: dict) -> float | None:
    st = item.get("state") or {}
    off, dur = st.get("timeOffset") or 0, st.get("duration") or 0
    if not dur or not off:
        return None
    p = off / dur
    return round(p, 3) if STARTED_AT <= p < core.FINISHED_AT else None


def _watched(item: dict) -> bool:
    st = item.get("state") or {}
    if item.get("type") == "movie":
        return bool(st.get("flaggedWatched") or st.get("timesWatched")) and not _progress(item)
    return False    # a series is never "done"; its card shows the episode it is on instead


def library_card(item: dict) -> dict:
    """A 16:9 card for a library item, plus the badge the Library tab shows (✓ watched, progress, episode)."""
    st = item.get("state") or {}
    c = core.card({"id": item.get("_id"), "type": item.get("type"), "name": item.get("name"),
                   "poster": item.get("poster")})
    c["lib"] = {"watched": _watched(item), "progress": _progress(item),
                "episode": core.episode_label(st.get("video_id")) if item.get("type") == "series" else None}
    return c


def _last_watched(item: dict) -> str:
    return (item.get("state") or {}).get("lastWatched") or item.get("_mtime") or ""


def library_rows(library: list[dict], recent_max: int = RECENT_MAX) -> list[dict]:
    """Library tab rows: Recently Added (by _ctime, when the title entered the account), then Series and Movies
    (last watched first). Only titles in the library (not removed, not temp) of movie/series type."""
    items = [it for it in library if in_library(it) and it.get("type") in core.TYPES and it.get("_id")]
    rows = []
    recent = sorted(items, key=lambda it: it.get("_ctime") or it.get("_mtime") or "", reverse=True)[:recent_max]
    if recent:
        rows.append({"key": "library|recent", "title": "Recently Added", "type": None,
                     "items": [library_card(it) for it in recent]})
    for t, label in (("series", "Series"), ("movie", "Movies")):
        group = sorted((it for it in items if it.get("type") == t), key=_last_watched, reverse=True)
        if group:
            rows.append({"key": f"library|{t}", "title": label, "type": t, "items": [library_card(it) for it in group]})
    return rows
