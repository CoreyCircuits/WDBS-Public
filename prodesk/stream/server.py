"""WDBS Stream server - Channel 9's 10-foot interface on the app host (docs/WDBS-STREAM.md).

Serves the UI (www/) to the kiosk browser on the private app-host screen and runs playback in mpv on the same
screen. Listens on 127.0.0.1 only: nothing on the network can reach it; the TV sees it through Sunshine/Moonlight.

    python3 server.py                 # serve (wdbs-stream.service)
    python3 server.py login           # one-time: sign in to your Stremio account (prompts; password never stored)
    python3 server.py logout
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import library_core as lcore
import stream_core as core
import search_core
from collections import OrderedDict

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WWW = HERE / "www"
STATIC = {"/fonts/": REPO / "fs42_overlay/static/wdbsguide/fonts", "/img/": REPO / "fs42_overlay/static/wdbsguide/img"}
BIND = ("127.0.0.1", int(os.environ.get("WDBS_STREAM_PORT", "8750")))
CONFIG = Path.home() / ".config/wdbs"
AUTH_FILE = CONFIG / "stremio-auth.json"
API = "https://api.strem.io/api/"
CINEMETA = "https://v3-cinemeta.strem.io/manifest.json"
UA = "WDBS-Stream/1.0"
APPHOST_DISPLAY = os.environ.get("WDBS_APPHOST_DISPLAY", "wayland-wdbs")
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
MPV_APP = "io.mpv.Mpv"
MPV_SOCK = RUNTIME / "app" / MPV_APP / "wdbs-stream.sock"          # visible inside the mpv Flatpak sandbox
MPV_CONF_DIR = Path.home() / ".var/app" / MPV_APP / "config/mpv"
MPV_OVERLAY = MPV_CONF_DIR / "wdbs"               # copy of mpv/ (wdbs.lua overlay + menus, fonts), refreshed per play
LOG = logging.getLogger("wdbs-stream")
PREFS = Path.home() / ".config/wdbs/stream-prefs.json"   # {"autoplay": true}: the player's Autoplay button


def prefs() -> dict:
    try:
        return {"autoplay": True, **json.loads(PREFS.read_text())}
    except (OSError, ValueError):
        return {"autoplay": True}


def set_pref(key: str, value) -> dict:
    p = {**prefs(), key: value}
    PREFS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREFS.with_suffix(".tmp")
    tmp.write_text(json.dumps(p))
    os.replace(tmp, PREFS)
    return p
POOL = ThreadPoolExecutor(max_workers=12)

MPV_INPUT = """# WDBS Stream remote keys in the player (written by wdbs-stream; edits are overwritten)
# wdbs.lua (mpv/) takes these keys over with forced bindings; this file is the fallback if it fails to load.
ESC quit
BS quit
ENTER cycle pause; show-progress
KP_ENTER cycle pause; show-progress
SPACE cycle pause; show-progress
RIGHT seek 10; show-progress
LEFT seek -10; show-progress
UP seek 60; show-progress
DOWN seek -60; show-progress
PGUP seek 600; show-progress
PGDWN seek -600; show-progress
"""


# ------------------------------------------------------------------ HTTP helpers (with a small TTL cache)
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def get_json(url: str, ttl: float = 600, timeout: float = 12):
    with _cache_lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    with _cache_lock:
        _cache[url] = (time.time(), data)
    return data


def api(method: str, payload: dict, timeout: float = 15):
    req = urllib.request.Request(API + method, data=json.dumps(payload).encode(), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    if data.get("error"):
        raise RuntimeError(f"Stremio API {method}: {data['error'].get('message') if isinstance(data['error'], dict) else data['error']}")
    return data.get("result")


# ------------------------------------------------------------------ Stremio account
class Account:
    def __init__(self):
        self.lock = threading.Lock()
        self._addons: tuple[float, list] | None = None
        self._library: tuple[float, list] | None = None

    @property
    def auth_key(self) -> str | None:
        try:
            return json.loads(AUTH_FILE.read_text()).get("authKey")
        except (OSError, ValueError):
            return None

    def addons(self) -> list[dict]:
        with self.lock:
            if self._addons and time.time() - self._addons[0] < 600:
                return self._addons[1]
        key = self.auth_key
        if key:
            try:
                addons = api("addonCollectionGet", {"type": "AddonCollectionGet", "authKey": key, "update": True})["addons"]
            except (OSError, RuntimeError, KeyError, TypeError, ValueError) as e:
                LOG.warning("addon collection: %s (using Cinemeta only)", e)
                addons = None
        else:
            addons = None
        if not addons:
            addons = [{"transportUrl": CINEMETA, "manifest": get_json(CINEMETA, ttl=86400)}]
        with self.lock:
            self._addons = (time.time(), addons)
        return addons

    def library(self, fresh: bool = False) -> list[dict]:
        key = self.auth_key
        if not key:
            return []
        with self.lock:
            if not fresh and self._library and time.time() - self._library[0] < 60:
                return self._library[1]
        try:
            lib = api("datastoreGet", {"authKey": key, "collection": "libraryItem", "ids": [], "all": True}) or []
        except (OSError, RuntimeError, ValueError) as e:
            LOG.warning("library: %s", e)
            lib = self._library[1] if self._library else []
        with self.lock:
            self._library = (time.time(), lib)
        return lib

    def library_item(self, id_: str) -> dict | None:
        return next((it for it in self.library() if it.get("_id") == id_), None)

    def put_item(self, item: dict) -> None:
        key = self.auth_key
        if not key:
            return
        api("datastorePut", {"authKey": key, "collection": "libraryItem", "changes": [item]})
        with self.lock:
            self._library = None


ACCOUNT = Account()


# ------------------------------------------------------------------ views
def fetch_row(row: dict) -> dict:
    try:
        data = get_json(core.resource_url(row["transportUrl"], "catalog", row["type"], row["id"]))
        items = [core.card(m) for m in (data.get("metas") or [])[:30] if m.get("id")]
    except (OSError, ValueError, urllib.error.URLError) as e:
        LOG.warning("catalog %s: %s", row["key"], e)
        items = []
    return {"key": row["key"], "title": row["title"], "type": row["type"], "items": items}


def home(tab: str) -> dict:
    addons = ACCOUNT.addons()
    lib = ACCOUNT.library()
    out = {"signedIn": bool(ACCOUNT.auth_key), "continue": [], "rows": []}
    if tab == "library":
        out["rows"] = lcore.library_rows(lib)                       # library_core.py: Recently Added / Series / Movies
        return out
    type_filter = {"movies": "movie", "tv": "series"}.get(tab)
    if tab == "home":
        out["continue"] = core.continue_watching(lib)
    rows = core.catalog_rows(addons, type_filter)
    out["rows"] = [r for r in POOL.map(fetch_row, rows) if r["items"]]
    return out


def meta(type_: str, id_: str) -> dict:
    for a in core.meta_addons(ACCOUNT.addons(), "meta", type_, id_) or [{"transportUrl": CINEMETA}]:
        try:
            m = get_json(core.resource_url(a["transportUrl"], "meta", type_, id_)).get("meta")
        except (OSError, ValueError, urllib.error.URLError) as e:
            LOG.warning("meta %s from %s: %s", id_, a["transportUrl"], e)
            continue
        if m:
            break
    else:
        raise LookupError(f"no metadata for {id_}")
    c = core.card(m)
    c["cast"] = (m.get("cast") or [])[:4]
    c["director"] = (m.get("director") or [])[:2]
    c["trailers"] = core.trailers(m)                       # [{title, ytId}] (trailers: WDBS-STREAM.md §3)
    seasons: dict[int, list] = {}
    for v in m.get("videos") or []:
        s = v.get("season")
        if not isinstance(s, int) or not isinstance(v.get("episode"), int):
            continue
        seasons.setdefault(s, []).append({"id": v.get("id"), "season": s, "episode": v["episode"],
                                          "title": v.get("title") or v.get("name") or f"Episode {v['episode']}",
                                          "thumbnail": v.get("thumbnail"), "released": (v.get("released") or "")[:10],
                                          "overview": (v.get("overview") or v.get("description") or "")[:300]})
    c["seasons"] = [{"season": s, "episodes": sorted(eps, key=lambda e: e["episode"])}
                    for s, eps in sorted(seasons.items(), key=lambda kv: (kv[0] == 0, kv[0]))]
    item = ACCOUNT.library_item(id_)
    c["inLibrary"] = lcore.in_library(item)                          # detail page "+ Library" button
    st = (item or {}).get("state") or {}
    resume_vid = st.get("video_id") or (id_ if type_ == "movie" else None)
    c["resume"] = {"videoId": resume_vid, "offsetS": core.resume_offset(item, resume_vid) if resume_vid else 0,
                   "episode": core.episode_label(resume_vid)} if resume_vid else None
    return c


def library_set(body: dict) -> dict:
    """POST /api/library {id, type, name, poster, add}: add to / remove from the owner's Stremio library (syncs to
    every Stremio device). Reads that one item fresh from the account first so a stale cache never overwrites it."""
    id_, type_, add = body.get("id"), body.get("type"), body.get("add")
    if not isinstance(id_, str) or not id_ or type_ not in core.TYPES or not isinstance(add, bool):
        raise ValueError("need id, type (movie/series) and add (true/false)")
    key = ACCOUNT.auth_key
    if not key:
        raise PermissionError("Sign in to Stremio first (server.py login).")
    got = api("datastoreGet", {"authKey": key, "collection": "libraryItem", "ids": [id_], "all": False}) or []
    item = next((it for it in got if isinstance(it, dict) and it.get("_id") == id_), None)
    meta = {"id": id_, "type": type_, "name": str(body.get("name") or ""), "poster": body.get("poster") or None}
    new = lcore.set_in_library(item, meta, add, datetime.now(timezone.utc))
    if new is not None:
        ACCOUNT.put_item(new)                                           # also drops the server's library cache
        LOG.info("library: %s %s (%s)", "added" if add else "removed", id_, meta["name"])
    return {"id": id_, "inLibrary": lcore.in_library(new if new is not None else item), "changed": new is not None}


def streams(type_: str, video_id: str) -> list[dict]:
    addons = core.meta_addons(ACCOUNT.addons(), "stream", type_, video_id)

    def one(a):
        name = (a.get("manifest") or {}).get("name") or "addon"
        try:
            data = get_json(core.resource_url(a["transportUrl"], "stream", type_, video_id), ttl=300, timeout=15)
            return [(name, s) for s in data.get("streams") or [] if isinstance(s, dict)]
        except (OSError, ValueError, urllib.error.URLError) as e:
            LOG.warning("streams from %s: %s", name, e)
            return []

    pairs = [p for chunk in POOL.map(one, addons) for p in chunk]
    return core.rank_streams(pairs)


# ------------------------------------------------------------------ search (GET /api/search?q=; logic in search_core)
# Every typed letter is a new query, so search answers get their own small bounded cache instead of get_json's.
_search_cache: "OrderedDict[str, tuple[float, list]]" = OrderedDict()
_search_lock = threading.Lock()


def search(q: str | None) -> dict:
    query = search_core.normalize_query(q)
    if not query:
        return {"query": query or "", "rows": []}
    key = query.lower()
    with _search_lock:
        hit = _search_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            _search_cache.move_to_end(key)
            return {"query": query, "rows": hit[1]}

    def one(cat):
        try:
            req = urllib.request.Request(search_core.search_url(cat, query),
                                         headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return cat, (json.load(r).get("metas") or [])
        except (OSError, ValueError, AttributeError, urllib.error.URLError) as e:
            LOG.warning("search %s: %s", cat["key"], e)
            return cat, []

    rows = search_core.merge_results(list(POOL.map(one, search_core.search_catalogs(ACCOUNT.addons()))), query)
    with _search_lock:
        _search_cache[key] = (time.time(), rows)
        while len(_search_cache) > 64:
            _search_cache.popitem(last=False)
    return {"query": query, "rows": rows}


# ------------------------------------------------------------------ memory caps (incident 9/29 00:24, WDBS-STREAM.md §8)
# `flatpak run` puts the app in its own app-flatpak-<id>-<n>.scope, outside wdbs-ch9.slice, so its cap is set here.
MPV_MEMORY = {"MemoryHigh": "700M", "MemoryMax": "1G", "MemorySwapMax": "256M"}
# light dynamic normaliser: lifts quiet dialogue after the stereo downmix, never above -1 dBFS peaks
AUDIO_FILTER = "dynaudnorm=f=250:g=15:p=0.89:m=8"


def cap_flatpak_scope(app_id: str, limits: dict, wait_s: float = 8.0) -> bool:
    """Set runtime memory limits on the newest running scope of a Flatpak app. Never raises."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            out = subprocess.run(["systemctl", "--user", "list-units", "--type=scope", "--state=running", "--plain",
                                  "--no-legend", f"app-flatpak-{app_id}-*.scope"],
                                 capture_output=True, text=True, timeout=3).stdout.split()
            scopes = [w for w in out if w.startswith(f"app-flatpak-{app_id}-") and w.endswith(".scope")]
            if scopes:   # newest by start time (instance ids are random, list order means nothing)
                def started(sc):
                    r = subprocess.run(["systemctl", "--user", "show", sc, "-p", "ActiveEnterTimestampMonotonic",
                                        "--value"], capture_output=True, text=True, timeout=3).stdout.strip()
                    return int(r) if r.isdigit() else 0
                scope = max(scopes, key=started)
                subprocess.run(["systemctl", "--user", "set-property", "--runtime", scope,
                                *[f"{k}={v}" for k, v in limits.items()]], capture_output=True, timeout=5, check=True)
                LOG.info("memory cap %s on %s", " ".join(f"{k}={v}" for k, v in limits.items()), scope)
                return True
        except (OSError, subprocess.SubprocessError) as e:
            LOG.warning("memory cap for %s: %s", app_id, e)
            return False
        time.sleep(0.3)
    LOG.warning("memory cap for %s: no scope appeared", app_id)
    return False


# ------------------------------------------------------------------ Ch 9 audio (owner 9/29: never on the app host's speakers)
# Everything on the app-host screen plays into a private null sink; Sunshine streams that sink's monitor
# (sunshine.conf audio_sink). Created by wdbs-apphost (prodesk/apphost/audio-sink.sh); nothing reaches the speakers.
AUDIO_SINK = os.environ.get("WDBS_AUDIO_SINK", "wdbs_ch9")
SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"
TORRENT_SERVER = ("127.0.0.1", 11470)


def torrent_server_up(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(TORRENT_SERVER, timeout=timeout):
            return True
    except OSError:
        return False


def ensure_torrent_server(wait_s: float = 25.0) -> bool:
    """Torrent-only sources play through stock Stremio's streaming server, which runs only on demand (it costs
    ~400 MB). Start wdbs-stremio and wait for :11470."""
    if torrent_server_up():
        return True
    LOG.info("starting wdbs-stremio for its torrent streaming server")
    subprocess.run(["systemctl", "--user", "start", "wdbs-stremio.service"], capture_output=True, timeout=20)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if torrent_server_up():
            return True
        time.sleep(0.5)
    return False


def watch_sunshine(player) -> None:
    """Pause playback when the TV's Moonlight session ends (tuned away from Ch 9, Wi-Fi drop, TV host restart), so
    nothing keeps playing unseen and the resume point stays where the viewer left it."""
    while True:
        try:
            proc = subprocess.Popen(["journalctl", "--user", "-u", SUNSHINE_UNIT, "-f", "-n", "0", "-o", "cat"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
            for line in proc.stdout:
                if "CLIENT DISCONNECTED" in line:
                    if player.pause():
                        LOG.info("TV left Channel 9: paused")
                elif "CLIENT CONNECTED" in line:
                    LOG.info("TV on Channel 9")
        except OSError as e:
            LOG.warning("sunshine watch: %s", e)
        time.sleep(5)


# ------------------------------------------------------------------ player (mpv on the app-host screen)
class Player:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.info: dict = {"state": "idle"}
        self.sources: list[dict] = []          # ranked sources for what is playing (overlay: Sources)

    def _ipc(self, *commands):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(MPV_SOCK))
            for i, c in enumerate(commands, 1):
                s.sendall((json.dumps({"command": c, "request_id": i}) + "\n").encode())
            got, buf = {}, b""
            while len(got) < len(commands):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    msg = json.loads(ln) if ln.strip() else {}
                    if msg.get("request_id"):
                        got[msg["request_id"]] = msg.get("data")
            return [got.get(i) for i in range(1, len(commands) + 1)]

    def state(self) -> dict:
        return dict(self.info)

    def pause(self) -> bool:
        """Pause if playing (idempotent: never toggles). True if it paused something."""
        if not self.proc or self.proc.poll() is not None:
            return False
        try:
            (was,) = self._ipc(["get_property", "pause"])
            if was is False:
                self._ipc(["set_property", "pause", True])
                return True
        except (OSError, ValueError):
            pass
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._ipc(["quit"])
            except OSError:
                self.proc.terminate()

    def play(self, req: dict) -> dict:
        with self.lock:
            self.stop()
            if self.proc:
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            MPV_CONF_DIR.mkdir(parents=True, exist_ok=True)
            (MPV_CONF_DIR / "wdbs-input.conf").write_text(MPV_INPUT)
            shutil.copytree(HERE / "mpv", MPV_OVERLAY, dirs_exist_ok=True)
            MPV_SOCK.parent.mkdir(parents=True, exist_ok=True)
            MPV_SOCK.unlink(missing_ok=True)
            trailer = req.get("kind") == "trailer"           # YouTube via the ytdl hook; never touches the library
            item = None if trailer else ACCOUNT.library_item(req["id"])
            start = core.resume_offset(item, req["videoId"]) if req.get("resume", True) and not trailer else 0.0
            if req.get("startAt") is not None:                 # a source switch continues where the last one was
                start = max(0.0, float(req["startAt"]))
            title = req.get("title") or req.get("name") or "WDBS Stream"
            argv = ["flatpak", "run", "--command=mpv", MPV_APP, "--no-config", "--fs", "--force-window=immediate",
                    f"--input-conf={MPV_CONF_DIR / 'wdbs-input.conf'}", "--input-default-bindings=no",
                    f"--input-ipc-server={MPV_SOCK}", "--hwdec=auto-safe", f"--audio-device=pulse/{AUDIO_SINK}", "--keep-open=no" if trailer else "--keep-open=yes", "--idle=no",
                    "--cache=yes", "--demuxer-max-bytes=128MiB", "--demuxer-readahead-secs=30",
                    "--network-timeout=30", f"--user-agent={UA}", "--alang=en,eng", "--slang=en,eng",
                    "--sub-auto=fuzzy", "--osd-level=1", "--osd-bar-h=2", "--osd-font-size=40",
                    "--osd-color=#F6F1E8", "--osd-bar-align-y=0.85", f"--force-media-title={title}",
                    "--background-color=#000000", "--cursor-autohide=always",
                    # WDBS overlay + Subtitles & Audio menu (mpv/wdbs.lua); subs only auto-on when audio isn't English
                    f"--script={MPV_OVERLAY / 'wdbs.lua'}", f"--osd-fonts-dir={MPV_OVERLAY / 'fonts'}", "--osc=no",
                    "--subs-with-matching-audio=forced",
                    # owner 9/29: a file's default-flagged foreign subtitle track must never switch on by itself
                    "--subs-fallback=no",
                    # owner 9/29 "low audio": stereo downmix + dynamic normalisation lifts quiet 5.1 dialogue
                    "--audio-channels=stereo", f"--af=lavfi=[{AUDIO_FILTER}]"]
            headers = req.get("headers") or {}
            if headers:
                argv.append("--http-header-fields=" + ",".join(f"{k}: {v}" for k, v in headers.items()
                                                               if "," not in f"{k}{v}"))
            if trailer:   # the Flatpak bundles yt-dlp; let it pick YouTube's own headers/user agent
                argv = [a for a in argv if not a.startswith("--user-agent=")]
                argv += ["--ytdl=yes", f"--ytdl-format={core.YTDL_FORMAT}"]
            if start > 0:
                argv.append(f"--start={start:.0f}")
            if "127.0.0.1:11470/" in req["url"] and not ensure_torrent_server():
                raise ValueError("torrent streaming server not available")
            argv += ["--", req["url"]]
            env = {**os.environ, "WAYLAND_DISPLAY": APPHOST_DISPLAY}
            env.pop("DISPLAY", None)
            self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, text=True, errors="replace")
            self.info = {"state": "starting", "title": title, "id": req["id"], "videoId": req["videoId"],
                         "startedAt": time.time(), "from": start, "kind": "trailer" if trailer else "title"}
            LOG.info("play %s (%s) from %.0f s via %s", req["videoId"], title, start, req.get("addon") or "?")
            threading.Thread(target=cap_flatpak_scope, args=(MPV_APP, MPV_MEMORY), daemon=True).start()
            self.sources = []
            if not trailer:
                threading.Thread(target=self._load_sources, args=(req,), daemon=True).start()
            threading.Thread(target=self._watch, args=(self.proc, req, start), daemon=True).start()
            return self.state()

    def _load_sources(self, req: dict) -> None:
        """Ranked sources for the Sources button (the UI fetched the same list moments ago, so it is cached)."""
        try:
            if str(req.get("id", "")).startswith("wdbs-test") and isinstance(req.get("testSources"), list):
                self.sources = req["testSources"][:core.OVERLAY_SOURCES]    # local overlay tests only
                return
            self.sources = streams(req["type"], req["videoId"])[:core.OVERLAY_SOURCES]
        except Exception as e:   # never break playback over the Sources list
            LOG.warning("sources for overlay: %s", e)

    def _push_sources(self, req: dict) -> bool:
        if not self.sources:
            return False
        try:
            self._ipc(["set_property", "user-data/wdbs/sources",
                       json.dumps(core.overlay_sources(self.sources, req["url"]), ensure_ascii=False)])
            return True
        except (OSError, ValueError):
            return False

    def _switch(self, req: dict, index: int, position: float) -> None:
        if not 0 <= index < len(self.sources):
            return
        s = self.sources[index]
        LOG.info("switch source for %s to #%d %s %s at %.0f s", req["videoId"], index + 1, s.get("quality"),
                 s.get("addon"), position)
        nxt = {**req, "url": s["url"], "headers": s.get("headers") or {}, "addon": s.get("addon"),
               "startAt": max(0.0, position - 2)}
        try:
            self.play(nxt)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            LOG.warning("source switch failed: %s", e)
            self.info = {"state": "error", "title": self.info.get("title"), "id": req["id"],
                         "videoId": req["videoId"], "error": "That source didn't play.", "kind": "title"}

    # ------------------------------------------------------------ up next / next + previous episode / autoplay
    def _episode_req(self, req: dict, step: int) -> dict | None:
        """The play request for the episode `step` away (+1 next, -1 previous), with the source autoplay picks."""
        if req.get("type") != "series" or str(req.get("id", "")).startswith("wdbs-"):
            return None
        m = meta("series", req["id"])
        ep = core.adjacent_episode(m.get("seasons") or [], req["videoId"], step)
        if not ep:
            return None
        src = core.pick_source(streams("series", ep["id"]), req.get("addon"))
        if not src:
            return None
        name = m.get("name") or req.get("name") or ""
        return {"id": req["id"], "type": "series", "videoId": ep["id"], "name": name,
                "poster": m.get("poster") or req.get("poster"), "title": core.episode_title(name, ep),
                "url": src["url"], "headers": src.get("headers") or {}, "addon": src.get("addon"),
                "resume": step > 0, "upnextLabel": f"S{ep['season']} E{ep['episode']}", "upnextTitle": ep.get("title")}

    def _related_req(self, req: dict) -> dict | None:
        """Autoplay after a movie: a well-rated movie of the same genre that isn't watched or in progress."""
        if req.get("type") != "movie" or str(req.get("id", "")).startswith("wdbs-"):
            return None
        genres = meta("movie", req["id"]).get("genres") or []
        skip = core.seen_ids(ACCOUNT.library())
        for g in genres[:2]:
            url = core.resource_url(CINEMETA, "catalog", "movie", "top", {"genre": g})
            try:
                metas = get_json(url, ttl=3600).get("metas") or []
            except (OSError, ValueError, urllib.error.URLError) as e:
                LOG.warning("related (%s): %s", g, e)
                continue
            c = core.related_movie(metas, req["id"], skip)
            if not c:
                continue
            src = core.pick_source(streams("movie", c["id"]), req.get("addon"))
            if src:
                return {"id": c["id"], "type": "movie", "videoId": c["id"], "name": c.get("name") or "",
                        "poster": c.get("poster"), "title": c.get("name") or "", "url": src["url"],
                        "headers": src.get("headers") or {}, "addon": src.get("addon"),
                        "upnextLabel": f"Because you watched {req.get('name') or 'that'}", "upnextTitle": c.get("name")}
        return None

    def _up_next(self, req: dict) -> dict | None:
        try:
            return self._episode_req(req, 1) or (self._related_req(req) if prefs()["autoplay"] else None)
        except Exception as e:   # never break the end of playback over a lookup
            LOG.warning("up next for %s: %s", req.get("videoId"), e)
            return None

    def _notice(self, text: str) -> None:
        try:
            self._ipc(["set_property", "user-data/wdbs/notice", text])
        except (OSError, ValueError):
            pass

    def _jump(self, req: dict, step: int) -> None:
        """⏮ / ⏭ while playing: previous / next episode."""
        try:
            nxt = self._episode_req(req, step)
        except Exception as e:
            LOG.warning("episode jump: %s", e)
            nxt = None
        if not nxt:
            self._notice("No previous episode" if step < 0 else "That's the last episode")
            return
        LOG.info("%s episode: %s -> %s", "next" if step > 0 else "previous", req["videoId"], nxt["videoId"])
        try:
            self.play(nxt)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            LOG.warning("episode jump failed: %s", e)
            self.info = {"state": "error", "title": nxt["title"], "id": nxt["id"], "videoId": nxt["videoId"],
                         "error": "That episode didn't play.", "kind": "title"}

    def _watch(self, proc, req, start):
        """Poll position every 5 s; save to the Stremio library every 60 s and when playback ends (not trailers)."""
        trailer = req.get("kind") == "trailer"
        pos, dur, watched, last_pos, last_save = start, 0.0, 0.0, None, time.time()
        err_tail: list[str] = []
        threading.Thread(target=lambda: err_tail.extend(proc.stderr.readlines()[-20:]), daemon=True).start()
        pushed, switched = False, False
        upnext, upnext_until, upnext_future = None, 0.0, None   # Up next card state (titles only)
        if not trailer:
            try:
                self._ipc(["set_property", "user-data/wdbs/autoplay", "on" if prefs()["autoplay"] else "off"])
            except (OSError, ValueError):
                pass
            if req.get("type") == "series":
                def label():
                    try:
                        nxt = core.adjacent_episode((meta("series", req["id"]).get("seasons") or []), req["videoId"], 1)
                        if nxt:
                            self._ipc(["set_property", "user-data/wdbs/next", f"S{nxt['season']} E{nxt['episode']}"])
                    except Exception:
                        pass
                threading.Thread(target=label, daemon=True).start()
        while proc.poll() is None:
            time.sleep(1)                                   # 1 s: overlay requests (Sources) feel immediate
            try:
                p, d, paused, request = self._ipc(["get_property", "time-pos"], ["get_property", "duration"],
                                                  ["get_property", "pause"], ["get_property", "user-data/wdbs/request"])
            except (OSError, ValueError):
                continue
            if not pushed and not trailer and isinstance(p, (int, float)):
                pushed = self._push_sources(req)
            if isinstance(request, str) and request in ("next", "prev", "autoplay:toggle", "upnext:go", "upnext:cancel"):
                try:
                    self._ipc(["set_property", "user-data/wdbs/request", ""])
                except (OSError, ValueError):
                    pass
                if request == "autoplay:toggle":
                    on = not prefs()["autoplay"]
                    set_pref("autoplay", on)
                    LOG.info("autoplay %s", "on" if on else "off")
                    try:
                        self._ipc(["set_property", "user-data/wdbs/autoplay", "on" if on else "off"])
                    except (OSError, ValueError):
                        pass
                    if upnext and not on:
                        upnext_until = time.time() + core.UPNEXT_WAIT_S
                    continue
                if request == "upnext:cancel":
                    self._ipc(["quit"])
                    continue
                if request == "upnext:go" and upnext:
                    switched = True
                    threading.Thread(target=self.play, args=(upnext,), daemon=True).start()
                    upnext, upnext_until = None, float("inf")   # started: the countdown must not fire again
                    continue
                if request in ("next", "prev") and not trailer and req.get("type") == "series":
                    if isinstance(p, (int, float)):
                        pos = p
                    if dur and watched > 0:
                        self._save(req, pos, dur, watched)
                        watched = 0.0
                    switched = True
                    threading.Thread(target=self._jump, args=(req, 1 if request == "next" else -1), daemon=True).start()
                continue
            if not trailer and not switched and upnext is None and upnext_future is None and isinstance(p, (int, float)) and dur \
                    and dur - p < 90:
                upnext_future = POOL.submit(self._up_next, req)           # look it up during the credits
            if not trailer and not switched and upnext is None:
                try:
                    eof = self._ipc(["get_property", "eof-reached"])[0]
                except (OSError, ValueError):
                    eof = False
                if eof:
                    if dur and watched >= 0:
                        self._save(req, dur, dur, watched)
                        watched = 0.0
                    if upnext_future is None:
                        upnext_future = POOL.submit(self._up_next, req)
                    try:
                        upnext = upnext_future.result(timeout=30)
                    except Exception:
                        upnext = None
                    auto = prefs()["autoplay"]
                    if not upnext or (req.get("type") == "movie" and not auto):
                        self._ipc(["quit"])
                        continue
                    upnext_until = time.time() + (core.UPNEXT_COUNTDOWN_S if auto else core.UPNEXT_WAIT_S)
                    self.info["state"] = "upnext"
                    self.info["upnext"] = {"label": upnext.get("upnextLabel"), "title": upnext.get("upnextTitle")}
                    self._ipc(["set_property", "user-data/wdbs/upnext", json.dumps(
                        {"label": upnext.get("upnextLabel"), "title": upnext.get("upnextTitle"), "auto": auto,
                         "until": upnext_until}, ensure_ascii=False)])
                    LOG.info("up next after %s: %s (%s)", req["videoId"], upnext["videoId"], "auto" if auto else "waiting")
                    continue
            if upnext:
                if time.time() >= upnext_until:
                    if prefs()["autoplay"]:
                        switched = True
                        threading.Thread(target=self.play, args=(upnext,), daemon=True).start()
                        upnext, upnext_until = None, float("inf")
                    else:
                        self._ipc(["quit"])
                continue
            if isinstance(request, str) and request.startswith("source:"):
                try:
                    self._ipc(["set_property", "user-data/wdbs/request", ""])
                except (OSError, ValueError):
                    pass
                if isinstance(p, (int, float)):
                    pos = p
                if not trailer and dur and watched > 0:
                    self._save(req, pos, dur, watched)
                    watched = 0.0
                index = request.split(":", 1)[1]
                if index.isdigit():
                    switched = True
                    threading.Thread(target=self._switch, args=(req, int(index), pos), daemon=True).start()
                continue
            if isinstance(p, (int, float)):
                if self.info.get("state") == "starting":
                    self.info["state"] = "playing"
                if last_pos is not None and not paused and 0 < p - last_pos <= 30:
                    watched += p - last_pos
                last_pos, pos = p, p
            if isinstance(d, (int, float)):
                dur = d
            self.info.update(position=pos, duration=dur, paused=bool(paused))
            if not trailer and time.time() - last_save > 60 and watched > 0:
                self._save(req, pos, dur, watched)
                watched, last_save = 0.0, time.time()
        code = proc.returncode
        if switched or self.proc is not proc:              # replaced by a source switch: the new watcher reports
            if not trailer and dur and watched > 0:
                self._save(req, pos, dur, watched)
            return
        if self.info.get("state") == "starting" or (code and pos <= start + 1):
            self.info = {"state": "error", "title": self.info.get("title"), "id": req["id"], "videoId": req["videoId"],
                         "error": "Trailer unavailable." if trailer else "This source didn't play.",
                         "kind": "trailer" if trailer else "title"}
            LOG.warning("mpv ended without playing (code %s): %s", code, " | ".join(x.strip() for x in err_tail[-3:]))
            return
        if dur and watched >= 0 and not trailer:
            self._save(req, pos, dur, watched)
        self.info = {"state": "ended", "title": self.info.get("title"), "id": req["id"], "videoId": req["videoId"],
                     "position": pos, "duration": dur}
        LOG.info("stopped %s at %.0f / %.0f s", req["videoId"], pos, dur)

    def _save(self, req, pos, dur, watched):
        if str(req.get("id", "")).startswith("wdbs-"):     # local test clips never reach the owner's Stremio account
            return
        try:
            item = ACCOUNT.library_item(req["id"]) or core.new_library_item(
                {"id": req["id"], "name": req.get("name"), "type": req.get("type"), "poster": req.get("poster")},
                datetime.now(timezone.utc))
            ACCOUNT.put_item(core.progress_update(item, req["videoId"], pos, dur, watched, datetime.now(timezone.utc)))
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            LOG.warning("progress not saved: %s", e)


PLAYER = Player()


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "wdbs-stream"
    sys_version = ""

    def _json(self, code: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path) -> None:
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".woff2": "font/woff2", ".png": "image/png", ".svg": "image/svg+xml"}
        try:
            data = path.read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path: str) -> None:
        for prefix, root in STATIC.items():
            if path.startswith(prefix):
                target = (root / path[len(prefix):]).resolve()
                return self._file(target) if target.is_relative_to(root.resolve()) else self._json(404, {})
        target = (WWW / (path.lstrip("/") or "index.html")).resolve()
        return self._file(target) if target.is_relative_to(WWW.resolve()) else self._json(404, {})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/home":
                return self._json(200, home(q.get("tab", "home")))
            if u.path == "/api/meta":
                return self._json(200, meta(q["type"], q["id"]))
            if u.path == "/api/streams":
                return self._json(200, {"streams": streams(q["type"], q["id"])})
            if u.path == "/api/player":
                return self._json(200, PLAYER.state())
            if u.path == "/api/search":
                return self._json(200, search(q.get("q")))
        except KeyError as e:
            return self._json(400, {"error": f"missing {e}"})
        except LookupError as e:
            return self._json(404, {"error": str(e)})
        except Exception as e:  # never let a bad addon answer take the UI down
            LOG.exception("GET %s", u.path)
            return self._json(502, {"error": str(e)[:200]})
        return self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 65536)) or b"{}")
            if u.path == "/api/play":
                if body.get("kind") == "trailer":        # the URL is built here from a validated YouTube id only
                    body["url"] = core.trailer_url(body.get("ytId"))
                    body.pop("headers", None)
                elif not str(body.get("url", "")).startswith(("http://", "https://")):
                    return self._json(400, {"error": "bad url"})
                return self._json(200, PLAYER.play(body))
            if u.path == "/api/pause":
                return self._json(200, {"paused": PLAYER.pause()})
            if u.path == "/api/stop":
                PLAYER.stop()
                return self._json(200, PLAYER.state())
            if u.path == "/api/library":
                return self._json(200, library_set(body))
        except (ValueError, KeyError) as e:
            return self._json(400, {"error": str(e)[:200]})
        except PermissionError as e:
            return self._json(403, {"error": str(e)})
        except (OSError, RuntimeError) as e:                          # Stremio API unreachable / refused
            LOG.warning("POST %s: %s", u.path, e)
            return self._json(502, {"error": str(e)[:200]})
        return self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def login() -> int:
    email = input("Stremio email: ").strip()
    password = getpass.getpass("Stremio password (not shown, not stored): ")
    try:
        res = api("login", {"type": "Login", "email": email, "password": password, "facebook": False})
    except (OSError, RuntimeError) as e:
        print(f"Sign-in failed: {e}")
        return 1
    finally:
        password = None
    CONFIG.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG, 0o700)
    tmp = AUTH_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"authKey": res["authKey"], "user": (res.get("user") or {}).get("_id")}, f)
    os.replace(tmp, AUTH_FILE)
    print("Signed in. WDBS Stream will use your addons and Continue Watching (restart: systemctl --user restart wdbs-stream).")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["login"]:
        return login()
    if argv[:1] == ["logout"]:
        AUTH_FILE.unlink(missing_ok=True)
        print("Signed out.")
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    LOG.info("WDBS Stream on http://%s:%d (signed in: %s)", *BIND, bool(ACCOUNT.auth_key))
    subprocess.run(["flatpak", "kill", MPV_APP], capture_output=True, timeout=10)   # no mpv from a previous run
    threading.Thread(target=watch_sunshine, args=(PLAYER,), daemon=True).start()
    ThreadingHTTPServer(BIND, Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
