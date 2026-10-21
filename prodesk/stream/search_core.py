"""WDBS Stream search - pure logic, no network (docs/WDBS-STREAM.md, "Search").

Search uses the Stremio addon protocol: a catalog whose `extra` lists {"name": "search"} (or whose legacy
`extraSupported` has "search") answers `{base}/catalog/{type}/{id}/search={query}.json`. This module picks those
catalogs from the account's addon collection, normalises the typed query and merges the answers into the rows the
TV shows (Movies, Series), deduplicated and capped.
"""

from __future__ import annotations

import re

import stream_core as core

MIN_QUERY = 2               # one letter matches half the catalog; wait for two
MAX_QUERY = 60
MAX_CATALOGS = 12           # search requests per query (one per addon and type)
MAX_PER_ROW = 30
ROW_TITLES = {"movie": "Movies", "series": "Series"}


def normalize_query(q: str | None) -> str | None:
    """Collapse whitespace and cap the length; None when too short to search."""
    q = " ".join(str(q or "").split())[:MAX_QUERY].strip()
    return q if len(q) >= MIN_QUERY else None


def _extra_names(c: dict) -> tuple[set, set]:
    """(supported, required) extra names of a catalog, from `extra` plus the legacy fields."""
    supported, required = set(c.get("extraSupported") or []), set(c.get("extraRequired") or [])
    for e in c.get("extra") or []:
        if isinstance(e, dict) and e.get("name"):
            supported.add(e["name"])
            if e.get("isRequired"):
                required.add(e["name"])
    return supported, required


def search_catalogs(addons: list[dict], max_catalogs: int = MAX_CATALOGS) -> list[dict]:
    """Search-capable movie/series catalogs, in collection order: the first one per addon and type (an addon's other
    search catalogs of the same type usually search the same index), skipping any that also require another extra."""
    out, seen = [], set()
    for a in addons:
        m = a.get("manifest") or {}
        for c in m.get("catalogs") or []:
            t = c.get("type")
            if t not in core.TYPES or not a.get("transportUrl"):
                continue
            supported, required = _extra_names(c)
            if "search" not in supported or required - {"search"}:
                continue
            key = (a["transportUrl"], t)
            if key in seen:
                continue
            seen.add(key)
            out.append({"key": f"{m.get('id')}|{t}|{c.get('id')}", "transportUrl": a["transportUrl"], "type": t,
                        "id": c.get("id"), "addon": m.get("name") or m.get("id") or "addon"})
            if len(out) >= max_catalogs:
                return out
    return out


def search_url(catalog: dict, query: str) -> str:
    return core.resource_url(catalog["transportUrl"], "catalog", catalog["type"], catalog["id"], {"search": query})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def merge_results(results: list[tuple[dict, list[dict]]], query: str, per_row: int = MAX_PER_ROW) -> list[dict]:
    """[(catalog, metas)] -> [{title, type, items}]: one row per type, cards deduplicated by id (first addon wins,
    so collection order is the priority), capped. The row whose top hits match the query best comes first
    ("breaking bad" opens on Series), ties keep Movies first."""
    by_type: dict[str, list] = {t: [] for t in core.TYPES}
    seen: set = set()
    for cat, metas in results:
        t = cat.get("type")
        if t not in by_type:
            continue
        for m in metas or []:
            if not isinstance(m, dict) or not m.get("id") or m["id"] in seen or len(by_type[t]) >= per_row:
                continue
            seen.add(m["id"])
            by_type[t].append(core.card({**m, "type": m.get("type") or t}))
    q = _norm(query)

    def score(items):
        names = [_norm(it["name"]) for it in items[:3]]
        return 2 if q in names else 1 if any(n.startswith(q) for n in names) else 0

    rows = [{"title": ROW_TITLES[t], "type": t, "items": items} for t, items in by_type.items() if items]
    rows.sort(key=lambda r: -score(r["items"]))           # stable: equal scores keep Movies, Series
    return rows
