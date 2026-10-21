"""WDBS Stream core - pure logic, no network or processes (docs/WDBS-STREAM.md).

WDBS Stream is Channel 9's own 10-foot interface. Stremio supplies the ecosystem: the owner's account (addon
collection, including debrid-configured addons, and the library / Continue Watching) and the addon protocol
(catalogs, metadata, streams). This module turns those JSON documents into what the TV shows, ranks streams and
builds the library update that keeps Continue Watching in sync with every other Stremio device.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

METAHUB = "https://images.metahub.space"
TYPES = ("movie", "series")
MAX_ROWS = 10
CONTINUE_MAX = 20
FINISHED_AT = 0.90          # this much watched = finished (Stremio flags it watched, resume starts over)
RESUME_BACK_S = 3           # resume a little before where it stopped


# ------------------------------------------------------------------ addons
def base_url(transport_url: str) -> str:
    """https://x/manifest.json -> https://x"""
    return transport_url.rsplit("/manifest.json", 1)[0].rstrip("/")


def resource_url(transport_url: str, resource: str, type_: str, id_: str, extra: dict | None = None) -> str:
    path = f"{base_url(transport_url)}/{resource}/{quote(type_, safe='')}/{quote(id_, safe='')}"
    if extra:
        path += "/" + "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in extra.items())
    return path + ".json"


def _resources(manifest: dict) -> list[dict]:
    out = []
    for r in manifest.get("resources") or []:
        if isinstance(r, str):
            out.append({"name": r, "types": manifest.get("types") or [], "idPrefixes": manifest.get("idPrefixes")})
        elif isinstance(r, dict):
            out.append({"name": r.get("name"), "types": r.get("types") or manifest.get("types") or [],
                        "idPrefixes": r.get("idPrefixes", manifest.get("idPrefixes"))})
    return out


def supports(manifest: dict, resource: str, type_: str, id_: str) -> bool:
    for r in _resources(manifest):
        if r["name"] != resource or type_ not in r["types"]:
            continue
        prefixes = r["idPrefixes"]
        if not prefixes or any(id_.startswith(p) for p in prefixes):
            return True
    return False


def catalog_rows(addons: list[dict], type_filter: str | None = None, max_rows: int = MAX_ROWS) -> list[dict]:
    """Browsable catalogs (no required extra) of movie/series, in collection order."""
    rows = []
    for a in addons:
        m = a.get("manifest") or {}
        for c in m.get("catalogs") or []:
            t = c.get("type")
            if t not in TYPES or (type_filter and t != type_filter):
                continue
            extras = c.get("extra") or []
            if any(e.get("isRequired") for e in extras if isinstance(e, dict)) or "search" in (c.get("extraRequired") or []):
                continue
            label = "Movies" if t == "movie" else "Series"
            rows.append({"key": f"{m.get('id')}|{t}|{c.get('id')}", "transportUrl": a.get("transportUrl"),
                         "type": t, "id": c.get("id"), "title": f"{c.get('name') or m.get('name')} · {label}"})
            if len(rows) >= max_rows:
                return rows
    return rows


def meta_addons(addons: list[dict], resource: str, type_: str, id_: str) -> list[dict]:
    return [a for a in addons if supports(a.get("manifest") or {}, resource, type_, id_)]


# ------------------------------------------------------------------ artwork
def art(meta: dict) -> dict:
    """Landscape art, logo and poster for a meta preview (tvOS cards are 16:9)."""
    mid = str(meta.get("id") or "")
    imdb = mid.split(":", 1)[0] if mid.startswith("tt") else None
    return {
        "background": meta.get("background") or (f"{METAHUB}/background/medium/{imdb}/img" if imdb else None),
        "logo": meta.get("logo") or (f"{METAHUB}/logo/medium/{imdb}/img" if imdb else None),
        "poster": meta.get("poster") or (f"{METAHUB}/poster/medium/{imdb}/img" if imdb else None),
    }


def card(meta: dict) -> dict:
    """What a row card and the hero need from a meta preview."""
    runtime = meta.get("runtime")
    genres = meta.get("genres") or meta.get("genre") or []
    return {"id": meta.get("id"), "type": meta.get("type"), "name": meta.get("name") or "",
            "description": (meta.get("description") or "")[:420], "year": meta.get("releaseInfo") or meta.get("year"),
            "rating": meta.get("imdbRating"), "runtime": runtime, "genres": list(genres)[:3], **art(meta)}


# ------------------------------------------------------------------ library / Continue Watching
def continue_watching(library: list[dict], max_items: int = CONTINUE_MAX) -> list[dict]:
    """Stremio's Continue Watching: items (in the library, or temp = watched without adding) with a resume point."""
    items = []
    for it in library:
        st = it.get("state") or {}
        if it.get("type") not in TYPES or (it.get("removed") and not it.get("temp")):
            continue
        if not st.get("timeOffset") or not st.get("lastWatched"):
            continue
        items.append(it)
    items.sort(key=lambda it: it["state"]["lastWatched"], reverse=True)
    out = []
    for it in items[:max_items]:
        st = it["state"]
        c = card({"id": it.get("_id"), "type": it.get("type"), "name": it.get("name"), "poster": it.get("poster")})
        dur, off = st.get("duration") or 0, st.get("timeOffset") or 0
        c.update(videoId=st.get("video_id") or it.get("_id"), offsetS=off / 1000, durationS=dur / 1000,
                 progress=round(off / dur, 3) if dur else None, episode=episode_label(st.get("video_id")))
        out.append(c)
    return out


def library_list(library: list[dict], type_filter: str | None = None) -> list[dict]:
    items = [it for it in library if not it.get("removed") and not it.get("temp") and it.get("type") in TYPES
             and (not type_filter or it.get("type") == type_filter)]
    items.sort(key=lambda it: it.get("_mtime") or "", reverse=True)
    return [card({"id": it.get("_id"), "type": it.get("type"), "name": it.get("name"), "poster": it.get("poster")})
            for it in items]


def episode_label(video_id: str | None) -> str | None:
    """'tt123:2:5' -> 'S2 E5'"""
    parts = (video_id or "").split(":")
    if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
        return f"S{int(parts[-2])} E{int(parts[-1])}"
    return None


def resume_offset(item: dict | None, video_id: str) -> float:
    """Seconds to start at: the saved point for this exact video, unless (nearly) finished."""
    st = (item or {}).get("state") or {}
    if st.get("video_id") not in (video_id, None) or not st.get("timeOffset"):
        return 0.0
    off, dur = st["timeOffset"] / 1000, (st.get("duration") or 0) / 1000
    if dur and off / dur >= FINISHED_AT:
        return 0.0
    return max(0.0, off - RESUME_BACK_S)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def new_library_item(meta: dict, now: datetime) -> dict:
    """A temp library item (what Stremio creates when you watch something you didn't add to the library)."""
    ts = _iso(now)
    return {"_id": meta["id"], "name": meta.get("name") or "", "type": meta.get("type"),
            "poster": meta.get("poster"), "posterShape": "poster", "removed": True, "temp": True,
            "_ctime": ts, "_mtime": ts,
            "state": {"lastWatched": ts, "timeWatched": 0, "timeOffset": 0, "overallTimeWatched": 0,
                      "timesWatched": 0, "flaggedWatched": 0, "duration": 0, "video_id": None, "watched": None,
                      "noNotif": False},
            "behaviorHints": {"defaultVideoId": None, "featuredVideoId": None, "hasScheduledVideos": False}}


def progress_update(item: dict, video_id: str, position_s: float, duration_s: float, watched_s: float,
                    now: datetime) -> dict:
    """The library item after watching: resume point, time watched, finished flag. Unknown fields are kept."""
    it = {**item, "state": {**(item.get("state") or {})}}
    st, ts = it["state"], _iso(now)
    finished = bool(duration_s) and position_s / duration_s >= FINISHED_AT
    if st.get("video_id") != video_id:
        st["timeWatched"] = 0
    st["video_id"] = video_id
    st["lastWatched"] = ts
    st["duration"] = int(duration_s * 1000)
    st["timeOffset"] = 0 if finished else int(position_s * 1000)
    st["timeWatched"] = int((st.get("timeWatched") or 0) + watched_s * 1000)
    st["overallTimeWatched"] = int((st.get("overallTimeWatched") or 0) + watched_s * 1000)
    if finished:
        st["timesWatched"] = int(st.get("timesWatched") or 0) + 1
        if it.get("type") == "movie":
            st["flaggedWatched"] = 1
    it["_mtime"] = ts
    return it


# ------------------------------------------------------------------ streams
_CACHED = re.compile(r"(\[(RD|AD|PM|TB|DL|ED|OC|PKP)\+\]|⚡|\bcached\b|\bRD\+|\bAD\+|\bTB\+)", re.I)
_UNCACHED = re.compile(r"(\[(RD|AD|PM|TB|DL)\s?download\]|⏳)", re.I)
_RES = [(re.compile(r"\b(2160p|4k|uhd)\b", re.I), "4K", 10), (re.compile(r"\b1080p\b", re.I), "1080p", 30),
        (re.compile(r"\b720p\b", re.I), "720p", 15), (re.compile(r"\b(480p|sd)\b", re.I), "SD", 5)]
_BAD = re.compile(r"\b(cam|hdcam|ts|telesync|hdts|tc|scr)\b", re.I)
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", re.I)


def playable_url(stream: dict, torrent_server: str = "http://127.0.0.1:11470") -> str | None:
    """http(s) URLs play directly (debrid); a torrent needs Stremio's streaming server and a file index."""
    url = stream.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    ih = stream.get("infoHash")
    if isinstance(ih, str) and re.fullmatch(r"[0-9a-fA-F]{40}", ih) and isinstance(stream.get("fileIdx"), int):
        return f"{torrent_server}/{ih.lower()}/{stream['fileIdx']}"
    return None


# Language (owner 9/29: a Spanish release got picked). Torrentio-style names carry audio languages as flag emoji and
# release words; English (or unmarked) is neutral, foreign-only sinks far down, multi-audio a little. Never dropped.
_FLAG = re.compile("([\U0001F1E6-\U0001F1FF]{2})")
_EN_FLAGS = {"\U0001F1EC\U0001F1E7", "\U0001F1FA\U0001F1F8"}          # GB, US
_FOREIGN = [(re.compile(p, re.I), name) for p, name in (
    (r"\b(spanish|espa[nñ]ol|castellano|latino|lat\b|spa\b)", "Spanish"), (r"\b(truefrench|french|fran[cç]ais|vostfr|vff|vf)\b", "French"),
    (r"\b(german|deutsch|ger\b)", "German"), (r"\b(italian|italiano|ita\b)", "Italian"),
    (r"\b(portuguese|portugu[eê]s|pt-br|dublado)\b", "Portuguese"), (r"\b(hindi|tamil|telugu|malayalam|kannada)\b", "Indian"),
    (r"\b(russian|rus\b)", "Russian"), (r"\b(polish|pol\b)", "Polish"), (r"\b(turkish|tur\b)", "Turkish"),
    (r"\b(korean|kor\b)", "Korean"), (r"\b(japanese|jpn\b)", "Japanese"), (r"\b(chinese|mandarin|chi\b)", "Chinese"))]
_MULTI = re.compile(r"\b(multi|dual[ .-]?audio)\b", re.I)
FOREIGN_PENALTY, MULTI_PENALTY = 90, 12


def language(text: str) -> tuple[int, str | None]:
    """(score penalty, label) for a stream's name/title: English or unmarked = (0, None)."""
    flags = set(_FLAG.findall(text))
    words = [name for rx, name in _FOREIGN if rx.search(text)]
    foreign_flags = flags - _EN_FLAGS
    has_en = bool(flags & _EN_FLAGS) or bool(re.search(r"\b(english|eng)\b", text, re.I))
    if _MULTI.search(text) or (has_en and (foreign_flags or words)) or len(foreign_flags) > 1 and has_en:
        return MULTI_PENALTY, "Multi-language"
    if foreign_flags or words:
        return FOREIGN_PENALTY, words[0] if words else "Foreign language"
    return 0, None


def rank_streams(streams: list[tuple[str, dict]]) -> list[dict]:
    """[(addon name, stream)] -> playable options, best first: direct/cached debrid, then 1080p (the transport is
    1080p and the host encodes in software, so 4K costs more than it shows), then order given by the addon."""
    out = []
    for order, (addon, s) in enumerate(streams):
        url = playable_url(s)
        if not url:
            continue
        text = " ".join(str(s.get(k) or "") for k in ("name", "title", "description"))
        score, quality = 0, None
        if url.startswith(("http://", "https://")) and "127.0.0.1:11470" not in url:
            score += 100
        if _CACHED.search(text):
            score += 40
        if _UNCACHED.search(text):
            score -= 60
        for rx, label, pts in _RES:
            if rx.search(text):
                score, quality = score + pts, label
                break
        if _BAD.search(text):
            score -= 80
        penalty, lang = language(text)
        score -= penalty
        size = _SIZE.search(text)
        lines = [ln.strip() for ln in str(s.get("title") or s.get("description") or "").splitlines() if ln.strip()]
        out.append({"addon": addon, "name": " ".join(str(s.get("name") or addon).split()), "detail": lines[:3],
                    "quality": quality, "size": size.group(0) if size else None, "url": url,
                    "cached": bool(_CACHED.search(text)), "score": score, "order": order, "language": lang,
                    "headers": ((s.get("behaviorHints") or {}).get("proxyHeaders") or {}).get("request") or {}})
    out.sort(key=lambda x: (-x["score"], x["order"]))
    return out


OVERLAY_SOURCES = 8


def overlay_sources(ranked: list[dict], current_url: str) -> list[dict]:
    """The player overlay's Sources list: short labels, current one marked (index = position in `ranked`)."""
    out = []
    for s in ranked:
        label = " ".join(x for x in (s.get("quality") or "Source", "⚡" if s.get("cached") else "", s.get("size") or "") if x)
        detail = " · ".join(x for x in (s.get("language"), s.get("addon")) if x)
        out.append({"label": label, "detail": detail, "current": s.get("url") == current_url})
    return out


# ------------------------------------------------------------------ trailers (YouTube, played by mpv's ytdl hook)
_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_TRAILERS = 5
# The Flatpak mpv bundles yt-dlp. Cap at 1920x1080 (scope trailers are 1920x800; a height-only cap picks 2560x1066)
# and prefer H.264, then VP9: the host has no AV1 hardware decode and Sunshine already encodes in software.
YTDL_FORMAT = ("bv*[height<=1080][width<=1920][vcodec^=avc1]+ba/bv*[height<=1080][width<=1920][vcodec^=vp]+ba/"
               "bv*[height<=1080][width<=1920]+ba/b[height<=1080]/b")


def valid_yt_id(yt_id) -> bool:
    return isinstance(yt_id, str) and bool(_YT_ID.fullmatch(yt_id))


def trailers(meta: dict, max_items: int = MAX_TRAILERS) -> list[dict]:
    """Stremio meta -> [{title, ytId}], deduplicated, in addon order. Reads `trailerStreams` ([{title, ytId}], current
    Cinemeta) and the older `trailers` ([{source: ytId, type: "Trailer"}]); anything not a YouTube id is dropped."""
    name = meta.get("name") or "Trailer"
    out, seen = [], set()
    candidates = [(t.get("ytId"), t.get("title")) for t in meta.get("trailerStreams") or [] if isinstance(t, dict)]
    candidates += [(t.get("source"), None) for t in meta.get("trailers") or []
                   if isinstance(t, dict) and (t.get("type") or "Trailer") == "Trailer"]
    for yt_id, title in candidates:
        if not valid_yt_id(yt_id) or yt_id in seen:
            continue
        seen.add(yt_id)
        out.append({"title": str(title or name)[:120], "ytId": yt_id})
        if len(out) >= max_items:
            break
    return out


def trailer_url(yt_id: str) -> str:
    """The only URL a trailer play request may open (built server-side from a validated id)."""
    if not valid_yt_id(yt_id):
        raise ValueError("bad trailer id")
    return f"https://www.youtube.com/watch?v={yt_id}"


# ------------------------------------------------------------------ up next / autoplay (owner 2026-10-05)
UPNEXT_COUNTDOWN_S = 10      # autoplay on: the Up next card counts down this long, then the next title starts
UPNEXT_WAIT_S = 60           # autoplay off: the card waits this long for OK, then playback ends as before


def episode_order(seasons: list[dict]) -> list[dict]:
    """meta()['seasons'] -> episodes in watch order. Specials (season 0) are left out unless they are all there is."""
    regular = [e for s in seasons if s.get("season") != 0 for e in s.get("episodes") or []]
    eps = regular or [e for s in seasons for e in s.get("episodes") or []]
    return sorted(eps, key=lambda e: (e.get("season") or 0, e.get("episode") or 0))


def adjacent_episode(seasons: list[dict], video_id: str, step: int) -> dict | None:
    """The episode `step` away from video_id (+1 next, -1 previous) in watch order, or None at either end.
    A special (season 0) being watched has no neighbours: there is no sensible 'next' for it."""
    eps = episode_order(seasons)
    ids = [e.get("id") for e in eps]
    if video_id not in ids:
        return None
    i = ids.index(video_id) + step
    return eps[i] if 0 <= i < len(eps) else None


def episode_title(name: str, ep: dict | None) -> str:
    """The title string the UI passes to the player (mpv/wdbs.lua splits it on ' · ')."""
    if not ep:
        return name
    return f"{name} · S{ep.get('season')} E{ep.get('episode')} · {ep.get('title') or 'Episode ' + str(ep.get('episode'))}"


def pick_source(ranked: list[dict], prefer_addon: str | None = None) -> dict | None:
    """Autoplay's source: the best-ranked one from the addon that served the last title (same debrid/torrent setup),
    else the best overall. Never one ranked below zero (uncached debrid downloads, cams)."""
    good = [s for s in ranked if (s.get("score") or 0) >= 0]
    if prefer_addon:
        for s in good:
            if s.get("addon") == prefer_addon:
                return s
    return good[0] if good else None


def related_movie(candidates: list[dict], current_id: str, skip_ids: set[str]) -> dict | None:
    """First catalog movie that isn't the one that just ended and isn't watched or in progress (skip_ids)."""
    for c in candidates:
        cid = c.get("id")
        if cid and cid != current_id and cid not in skip_ids and c.get("type", "movie") == "movie":
            return c
    return None


def seen_ids(library: list[dict]) -> set[str]:
    """Library items already watched or started - autoplay should not offer them as 'related'."""
    out = set()
    for it in library:
        st = it.get("state") or {}
        if st.get("timesWatched") or st.get("flaggedWatched") or st.get("timeOffset"):
            out.add(it.get("_id"))
    return out
