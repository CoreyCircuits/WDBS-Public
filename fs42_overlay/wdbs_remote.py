"""WDBS Remote v2 API - served by FS42's own API server under /wdbs/remote (installed as fs42/wdbs_remote.py
by the wdbs-remote patch). Thin: all decisions live in fs42/wdbs_remote_core.py.

Ratings: ~/.local/state/wdbs/ratings.jsonl (one JSON object per line, append-only), copied to Notion by the
app host's wdbs-ratings-sync timer. A 👎 is held REASON_HOLD_S for a reason tap, then saved; a player
restart in that window loses it (at most one rating).
"""

import asyncio
import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from fs42 import wdbs_remote_core as core
from fs42.station_manager import StationManager

router = APIRouter(prefix="/wdbs/remote", tags=["wdbs-remote"])
GUARD = core.Guard()
STATE_DIR = Path(os.environ.get("WDBS_STATE_DIR", str(Path.home() / ".local/state/wdbs")))
RATINGS = STATE_DIR / "ratings.jsonl"
SLATE_STATE = STATE_DIR / "slate.json"
TODAY_FEED = os.environ.get("WDBS_TODAY_FEED", "http://app-host.example.invalid:8742/home.json")
KEY_LABELS = {"PageUp": "◀", "PageDown": "▶", "Enter": "OK",
              "ArrowUp": "▲", "ArrowDown": "▼", "ArrowLeft": "◀", "ArrowRight": "▶",
              "Back": "↩ BACK", "PlayPause": "⏯", "FastForward": "⏩", "Rewind": "⏪",
              "Space": "␣", "Backspace": "⌫", "Search": "🔍 SEARCH", "PrevTrack": "⏮", "NextTrack": "⏭", **{c: c.upper() for c in "abcdefghijklmnopqrstuvwxyz"},
              "ThumbsUp": "👍", "ThumbsDown": "👎"}
MPV_SOCKET = Path(os.environ.get("WDBS_MPV_SOCKET", "runtime/mpv.socket"))   # FS42 runs from its own root
MPV_TIMEOUT_S = 2.0
_feed = {"at": 0.0, "data": None}
_last = {"text": ""}
_pending: dict[str, dict] = {}
_thumbs = {"mtime": None, "map": {}}


def rated_now(status: dict) -> str | None:
    """'up'/'down' if what's on screen was already rated (latest wins, a held 👎 counts), else None."""
    key = core.content_key(status, None)
    if not key:
        return None
    try:
        mtime = RATINGS.stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _thumbs["mtime"]:
        recs = []
        if mtime is not None:
            for line in RATINGS.read_text(encoding="utf-8").split("\n"):
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
        _thumbs.update(mtime=mtime, map=core.latest_thumbs(recs))
    held = [r["thumb"] for r in _pending.values() if core.record_key(r) == key]
    return held[-1] if held else _thumbs["map"].get(key)


def read_status() -> dict:
    try:
        with open(StationManager().server_conf["status_socket"]) as f:
            return json.loads(f.read().strip() or "{}")
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def slate_up() -> str | None:
    try:
        return json.loads(SLATE_STATE.read_text()).get("slate")
    except (OSError, ValueError, AttributeError):
        return None


def mpv_ipc(*commands: list) -> list:
    """Run mpv commands over FS42's mpv IPC socket (one more client, like wdbs-slate); return each reply's data.
    Raises OSError/ValueError if mpv isn't reachable or doesn't answer within MPV_TIMEOUT_S."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(MPV_TIMEOUT_S)
        s.connect(str(MPV_SOCKET))
        for rid, cmd in enumerate(commands, start=1):
            s.sendall((json.dumps({"command": cmd, "request_id": rid}) + "\n").encode())
        replies, buf = {}, b""
        while len(replies) < len(commands):
            chunk = s.recv(65536)
            if not chunk:
                raise ValueError("mpv closed the socket")
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                msg = json.loads(line) if line.strip() else {}
                if msg.get("request_id") in range(1, len(commands) + 1):
                    replies[msg["request_id"]] = msg
    return [replies[i].get("data") for i in range(1, len(commands) + 1)]


def cycle_subtitles() -> str:
    """SUBS: next subtitle track, then OFF, then the first track again (logic: core.next_subtitle)."""
    try:
        tracks, visible, sid = mpv_ipc(["get_property", "track-list"], ["get_property", "sub-visibility"],
                                       ["get_property", "sid"])
        props, label = core.next_subtitle(tracks or [], bool(visible), sid)
        if props:
            mpv_ipc(*[["set_property", k, v] for k, v in props.items()])
            mpv_ipc(["show-text", label, 2000])
        return label
    except (OSError, ValueError, KeyError):
        return "SUBS unavailable"


def _tune(command: dict) -> None:
    with open(StationManager().server_conf["channel_socket"], "w") as f:
        f.write(json.dumps(command))


def _queue(request: Request, msg: dict) -> None:
    q = request.app.state.player_command_queue
    if not q:
        raise HTTPException(status_code=503, detail="player not connected")
    q.put(msg)


def schedule_context(status: dict, now: datetime):
    """(scheduled entry, next block (start, title)) for the channel on screen. Never raises."""
    try:
        from fs42.liquid_api import LiquidAPI
        station = StationManager().station_by_channel(status.get("channel_number"))
        if not station or station.get("network_type") in ("web", "guide"):
            return None, None
        blocks = LiquidAPI.get_blocks(station, now - timedelta(hours=1), now + timedelta(hours=3)) or []
        sched = nxt = None
        for b in blocks:
            if b.start_time <= now < b.end_time:
                plan = [(e.path, e.duration, getattr(e, "content_type", "") or "", getattr(e, "skip", 0) or 0)
                        for e in (b.plan or [])]
                sched = core.scheduled_entry(b.start_time, plan, now)
            elif b.start_time > now and nxt is None:
                nxt = (b.start_time, b.title)
        return sched, nxt
    except Exception:
        return None, None


def recent_log() -> list[str]:
    try:
        r = subprocess.run(["journalctl", "--user", "-u", "fs42-player", "--since", "-2min", "-o", "cat",
                            "--no-pager"], capture_output=True, text=True, timeout=4)
        return core.pick_log_lines(r.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return []


def today_feed():
    if time.time() - _feed["at"] < 60:
        return _feed["data"]
    try:
        with urllib.request.urlopen(TODAY_FEED, timeout=3) as r:
            data = json.load(r)
    except (OSError, ValueError):
        data = None
    _feed.update(at=time.time(), data=data)
    return data


def _flush(rid: str) -> None:
    rec = _pending.pop(rid, None)
    if rec:
        core.append_jsonl(RATINGS, rec)


async def _json_body(request: Request) -> dict:
    """A malformed body is a client error (400), not a 500 - the only client is our own page."""
    try:
        return await request.json()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")


@router.get("/")
async def home():
    return RedirectResponse("/static/wdbsremote/index.html")


@router.get("/state")
async def state():
    status = read_status()
    GUARD.observe(time.monotonic(), status.get("channel_number"))
    return {"ok": bool(status.get("channel_number")), "channel": status.get("channel_number"),
            "title": status.get("title"), "previous": GUARD.previous,
            "busy": GUARD.busy_since is not None and time.monotonic() - GUARD.busy_since < GUARD.settle_s,
            "last": _last["text"], "rated": rated_now(status)}


@router.get("/strip")
async def strip():
    feed = await asyncio.to_thread(today_feed)
    return {"day": core.day_strip(feed, datetime.now().astimezone()), "tv": core.tv_line(read_status())}


@router.post("/cmd")
async def cmd(request: Request):
    body = await _json_body(request)
    raw = str(body.get("cmd", ""))
    try:
        name, arg = core.parse_command(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    now = time.monotonic()
    status = read_status()
    GUARD.observe(now, status.get("channel_number"))
    why = GUARD.check(now, raw)
    if why:
        return {"ok": False, "result": why, "channel": GUARD.current}
    target = None
    if name == "tune":
        target = int(arg)
        _tune({"command": "direct", "channel": target})
        text = f"CH {target}"
    elif name in ("ch_up", "ch_down"):
        _tune({"command": "up" if name == "ch_up" else "down"})
        text = "CH ▲" if name == "ch_up" else "CH ▼"
    elif name == "guide":
        _queue(request, {"command": "guide"})
        g = StationManager().guide_config
        target = g["channel_number"] if g else None
        text = "GUIDE"
    elif name == "back":
        if GUARD.previous is None:
            return {"ok": False, "result": "no previous channel", "channel": GUARD.current}
        target = GUARD.previous
        _tune({"command": "direct", "channel": target})
        text = f"BACK · CH {target}"
    elif name == "info":
        GUARD.ran(now, raw, target)
        try:  # WDBS: wdbs-tuner shows the cable-box banner (tuning transition spec)
            tmp = Path("runtime/wdbs_info.json.tmp")
            tmp.write_text(json.dumps({"at": time.time()}))
            os.replace(tmp, "runtime/wdbs_info.json")
            text = "INFO"
        except OSError:
            text = "INFO unavailable"
        _last["text"] = text
        return {"ok": True, "result": text, "channel": GUARD.current}
    elif name == "key":
        _queue(request, {"command": "web_key", "key": arg})
        text = KEY_LABELS[arg]
    elif name in ("vol_up", "vol_down", "mute"):
        # WDBS: record before the await - a second tap must not pass check() while this one awaits
        GUARD.ran(now, raw, target)
        from fs42.fs42_server.api.player import _control_volume
        r = await _control_volume({"vol_up": "up", "vol_down": "down", "mute": "mute"}[name])
        text = str((r or {}).get("volume") or name.replace("_", " ").upper()) if isinstance(r, dict) else name.upper()
        _last["text"] = text
        return {"ok": True, "result": text, "channel": GUARD.current}
    else:  # subs
        # WDBS: record before the await - a second tap must not pass check() while this one awaits
        GUARD.ran(now, raw, target)
        text = await asyncio.to_thread(cycle_subtitles)
        _last["text"] = text
        return {"ok": True, "result": text, "channel": GUARD.current}
    GUARD.ran(now, raw, target)
    _last["text"] = text
    return {"ok": True, "result": text, "channel": GUARD.current}


@router.post("/rate")
async def rate(request: Request):
    body = await _json_body(request)
    now = datetime.now()
    status = read_status()
    if core.thumbs_to_page(status):
        # WDBS: this web page rates something of its own (Ch 5: the MilkDrop look) - hand it the key, keep no record
        thumb = body.get("thumb")
        if thumb not in ("up", "down"):
            raise HTTPException(status_code=400, detail="thumb must be up or down")
        _queue(request, {"command": "web_key", "key": "ThumbsUp" if thumb == "up" else "ThumbsDown"})
        return {"ok": True, "id": None, "label": "visualizer look", "thumb": thumb, "pending": False, "page": True}
    # WDBS: run concurrently - in series the two 4 s-capped fetches could outrun the page's 5 s abort
    (sched, _), log_lines = await asyncio.gather(asyncio.to_thread(schedule_context, status, now),
                                                  asyncio.to_thread(recent_log))
    try:
        rec = core.rating_record(rid=str(uuid.uuid4()), thumb=body.get("thumb"), reason=None, status=status,
                                 now_local=now, slate=slate_up(), sched=sched, log_lines=log_lines)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if rec["thumb"] == "down":
        _pending[rec["id"]] = rec
        asyncio.get_running_loop().call_later(core.REASON_HOLD_S, _flush, rec["id"])
        return {"ok": True, "id": rec["id"], "label": rec["label"], "thumb": rec["thumb"], "pending": True}
    core.append_jsonl(RATINGS, rec)
    return {"ok": True, "id": rec["id"], "label": rec["label"], "thumb": rec["thumb"], "pending": False}


@router.post("/rate/{rid}/reason")
async def rate_reason(rid: str, request: Request):
    body = await _json_body(request)
    rec = _pending.get(rid)
    if not rec:
        return {"ok": False, "result": "already saved", "label": None}
    try:
        _pending[rid] = core.with_reason(rec, str(body.get("reason", "")))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _flush(rid)
    return {"ok": True, "result": "saved", "label": rec["label"]}
