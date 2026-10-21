"""WDBS Remote v2 - pure logic, no FieldStation42 imports (installed as fs42/wdbs_remote_core.py by the
wdbs-remote patch; fs42/wdbs_remote.py wires it to FS42). Spec: docs/superpowers/specs/2026-09-28-remote-v2-design.md

Every remote (the phone page now, a physical remote later) goes through parse_command() and Guard.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

TUNES = {"tune", "ch_up", "ch_down", "guide", "back"}
COMMANDS = TUNES | {"info", "key", "vol_up", "vol_down", "mute", "subs"}
WEB_KEYS = {"PageUp", "PageDown", "Enter", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
            "Back", "PlayPause", "FastForward", "Rewind",   # Back: remote-app channels; ⏯ ⏩ ⏪ also reach web pages (web-keys-media)
            "ThumbsUp", "ThumbsDown"}                       # only sent by /rate on a THUMB_PAGES channel
# USB remote keyboard (wdbs-keys, 2026-10-05): letters, Space and Backspace type into the Ch 9 app's search;
# Search opens it. Web pages reject these in WebRender's own allowlist, so they only reach the remote-app channel.
TYPING_KEYS = frozenset("abcdefghijklmnopqrstuvwxyz") | {"Space", "Backspace", "Search",
                                                          "PrevTrack", "NextTrack"}   # ⏮ ⏭: Stream episodes
WEB_KEYS = WEB_KEYS | TYPING_KEYS
# Web channels whose page takes 👍/👎 itself (as ThumbsUp/ThumbsDown keys) instead of a rating record:
# Ch 5 WDBS RADIO rates the MilkDrop look on screen (vinyl-scope, 2026-09-30).
THUMB_PAGES = {"WDBS RADIO"}


def thumbs_to_page(status: dict) -> bool:
    """True when the channel on screen is a web page that handles 👍/👎 itself."""
    return str(status.get("network_name") or "").upper() in THUMB_PAGES
REASONS = ("Don't like it", "Glitch/stall", "Audio", "Bad cut", "Wrong spot")
REPEAT_S = 0.6        # the same command again within this is a double tap
KEY_REPEAT_S = 0.08   # navigation keys (key:*): only a phone's accidental double-fire; ▶ ▶ must both count (9/29)
RAPID = {"vol_up", "vol_down"}   # owner 9/29: rapid VOL presses all count (MUTE keeps the guard: a double toggles back)
SETTLE_S = 6.0        # a channel change counts as landed after this, reported or not
REASON_HOLD_S = 5.0   # a 👎 waits this long for a reason before it is saved plain


def parse_command(cmd: str) -> tuple[str, str | None]:
    """'tune:10' -> ('tune', '10'); 'key:PageUp' -> ('key', 'PageUp'); 'guide' -> ('guide', None).
    Anything else raises ValueError - there is no stop/exit command by design."""
    name, _, arg = cmd.partition(":")
    if name not in COMMANDS:
        raise ValueError(f"unknown command {cmd!r}")
    if name == "tune":
        if not arg.isdigit() or not 0 < int(arg) < 1000:
            raise ValueError(f"bad channel {arg!r}")
        return name, arg
    if name == "key":
        if arg not in WEB_KEYS:
            raise ValueError(f"key not allowed {arg!r}")
        return name, arg
    if arg:
        raise ValueError(f"{name} takes no argument")
    return name, None


@dataclass
class Guard:
    """One-at-a-time channel changes, double-tap filter, previous channel. Times are monotonic seconds."""
    repeat_s: float = REPEAT_S
    settle_s: float = SETTLE_S
    current: int | None = None
    previous: int | None = None
    last_cmd: str | None = None
    last_at: float = -1e9
    busy_since: float | None = None
    busy_from: int | None = None
    busy_target: int | None = None

    def observe(self, now: float, channel: int | None) -> None:
        """Feed the channel the player reports. Tracks previous channel; ends busy when a change lands."""
        if channel is not None and channel != self.current:
            if self.current is not None:
                self.previous = self.current
            self.current = channel
        if self.busy_since is not None:
            if self.busy_target is not None:
                landed = channel == self.busy_target
            else:
                landed = channel is not None and channel != self.busy_from
            if landed or now - self.busy_since >= self.settle_s:
                self.busy_since = None

    def check(self, now: float, cmd: str) -> str | None:
        """None if cmd may run now; 'repeat' for a double tap; 'busy' for a tune while one settles."""
        window = KEY_REPEAT_S if cmd.startswith("key:") or cmd in RAPID else self.repeat_s
        if cmd == self.last_cmd and now - self.last_at < window:
            return "repeat"
        if cmd.partition(":")[0] in TUNES and self.busy_since is not None and now - self.busy_since < self.settle_s:
            return "busy"
        return None

    def ran(self, now: float, cmd: str, target: int | None = None) -> None:
        self.last_cmd, self.last_at = cmd, now
        if cmd.partition(":")[0] in TUNES:
            self.busy_since, self.busy_from, self.busy_target = now, self.current, target


WEB_KINDS = {99: "guide", 3: "today", 4: "weather"}   # channel -> kind for web channels
DAYPARTS = [(1, 5, "Overnight"), (5, 8, "Early"), (8, 12, "Morning"), (12, 16, "Daytime"),
            (16, 19, "After School"), (19, 22, "Prime"), (22, 25, "Late Night")]      # 
_EP = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_RELEASE_JUNK = re.compile(r"\b(DVDRip|BDRip|BluRay|HDTV|WEB(-?DL|Rip)?|AMZN|NF|\d{3,4}p|x26[45]|XviD|DivX|"
                           r"AAC|AC3|DD5|H\.?26[45]|REPACK|PROPER)\b.*$", re.I)
_SPOT = re.compile(r"---(.+?)__\d+s__")
_LOG = re.compile(r"WARNING|ERROR|Traceback|Exception|timed out|failed", re.I)


def _episode(path: str) -> tuple[str | None, str | None]:
    stem = os.path.splitext(os.path.basename(path))[0]
    m = _EP.search(stem)
    if not m:
        return None, None
    ep = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    rest = stem[m.end():]
    title = rest.split(" - ", 1)[1] if " - " in rest else rest.replace(".", " ").replace("_", " ")
    title = _RELEASE_JUNK.sub("", title).strip(" -.")
    return ep, title or None


def episode_info(path: str) -> tuple[str | None, str | None]:
    """('S01E09', 'Personal Business') from an episode file name, or (None, None). Shared with wdbs.tuner_core."""
    return _episode(path)


def _show_tag(path: str) -> str | None:
    parts = path.split("/")
    if "catalog" in parts:
        i = parts.index("catalog")
        if len(parts) > i + 3:
            return parts[i + 2]
    return None


def classify(status: dict, slate: str | None) -> dict:
    """What is on screen, from FS42's player status (+ whether the WDBS standby slate is up)."""
    ch = status.get("channel_number")
    path = status.get("file_path") or ""
    ct = status.get("content_type") or ""
    out = {"kind": "unknown", "show_tag": None, "episode": None, "episode_title": None, "commercial_file": None}
    if ct == "web":
        out["kind"] = WEB_KINDS.get(ch, "web")
    elif ct == "remote_app":                    # Channel Router remote app (Ch 9 WDBS STREAM)
        out["kind"] = "app"
    elif ct == "commercial":
        out.update(kind="commercial", commercial_file=os.path.basename(path))
    elif ct == "weather" or path.startswith(":autobump:"):
        out["kind"] = "weather"
    elif ct == "bump":
        out["kind"] = "bump"
    elif path:
        ep, title = _episode(path)
        out.update(kind="episode", show_tag=_show_tag(path), episode=ep, episode_title=title)
    if slate:
        out["kind"] = "standby"
    return out


def _secs(hms: str) -> float:
    total = 0.0
    for part in hms.split(":"):
        total = total * 60 + float(part)
    return total


def fmt_hms(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def live_position(status: dict, now: datetime) -> tuple[float | None, float | None]:
    """FS42 writes 'offset/segment' once, when the entry starts, with a timestamp: position = offset + elapsed,
    capped at the segment end. `now` is naive local time, like FS42's timestamp."""
    dur, ts = status.get("duration"), status.get("timestamp")
    if not dur or "/" not in dur or not ts:
        return None, None
    try:
        off, seg = (_secs(x) for x in dur.split("/", 1))
        started = datetime.fromisoformat(ts)
    except ValueError:
        return None, None
    return min(off + max(0.0, (now - started).total_seconds()), off + seg), seg


def scheduled_entry(block_start: datetime, plan: list[tuple[str, float, str, float]], now: datetime) -> dict | None:
    """The plan entry the schedule says is on at `now`. plan items: (path, duration, content_type, skip)."""
    t = block_start
    for path, dur, ct, skip in plan:
        end = t + timedelta(seconds=dur or 0)
        if t <= now < end:
            return {"path": path, "content_type": ct, "planned_start": t.isoformat(timespec="seconds"), "skip": skip}
        t = end
    return None


def late_seconds(sched: dict | None, file_path: str | None, position_s: float | None, now: datetime) -> float | None:
    """How far playout runs behind the block timeline (only when the scheduled file is the one playing)."""
    if not sched or position_s is None or sched["path"] != file_path:
        return None
    planned = datetime.fromisoformat(sched["planned_start"])
    return round((now - planned).total_seconds() - (position_s - (sched.get("skip") or 0)), 1)


def daypart(dt: datetime) -> str:
    h = dt.hour + (24 if dt.hour < 1 else 0)
    return next(name for a, b, name in DAYPARTS if a <= h < b)


def pick_log_lines(lines: list[str], limit: int = 20) -> list[str]:
    return [line[:300] for line in lines if _LOG.search(line)][-limit:]


def _hm(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}"


def rating_label(rec: dict) -> str:
    """Short human name of what was rated: 'The Nanny S01E09', 'Zyrtec Drug Commercial', 'the guide'."""
    kind, title = rec.get("kind"), rec.get("title") or rec.get("network") or ""
    if kind == "standby":
        return f"standby slate over {title}".strip()
    if kind == "commercial":
        m = _SPOT.search(rec.get("commercial_file") or "")
        if not m:
            return os.path.splitext(rec.get("commercial_file") or "spot")[0]
        return re.sub(r"\s+", " ", m.group(1).replace("---", " · ").replace("-", " ")).strip()
    if kind == "episode":
        return f"{title} {rec.get('episode') or ''}".strip()
    return {"weather": "WeatherCast weather", "guide": "the guide", "today": "WDBS Today",
            "bump": "station ID / bumper", "app": f"{title or 'remote app'} (interactive)"}.get(kind, title or "unknown")


def info_text(status: dict, episode: str | None, episode_title: str | None,
              next_block: tuple[datetime, str] | None) -> str:
    parts = [f"Ch {status.get('channel_number')}", status.get("title") or status.get("network_name") or ""]
    if episode:
        parts.append(f"{episode} {episode_title}" if episode_title else episode)
    if next_block:
        parts.append(f"Next {_hm(next_block[0])} {next_block[1]}")
    return " · ".join(p for p in parts if p)


def rating_record(*, rid: str, thumb: str, reason: str | None, status: dict, now_local: datetime,
                  slate: str | None, sched: dict | None, log_lines: list[str]) -> dict:
    """One rating with its debug snapshot. `now_local` is naive local time (FS42's clock)."""
    if thumb not in ("up", "down"):
        raise ValueError(f"bad thumb {thumb!r}")
    if reason is not None and (thumb != "down" or reason not in REASONS):
        raise ValueError(f"bad reason {reason!r}")
    pos, seg = live_position(status, now_local)
    rec = {"id": rid, "at": now_local.astimezone().isoformat(timespec="seconds"), "daypart": daypart(now_local),
           "thumb": thumb, "reason": reason, "channel": status.get("channel_number"),
           "network": status.get("network_name"), "title": status.get("title"), **classify(status, slate),
           "file": status.get("file_path"), "position": fmt_hms(pos) if pos is not None else None,
           "position_s": pos, "segment_s": seg, "scheduled": sched,
           "late_s": late_seconds(sched, status.get("file_path"), pos, now_local), "slate": slate, "log": log_lines}
    rec["label"] = rating_label(rec)
    return rec


def with_reason(rec: dict, reason: str) -> dict:
    if rec.get("thumb") != "down" or reason not in REASONS:
        raise ValueError(f"bad reason {reason!r}")
    return {**rec, "reason": reason}


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _left(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min left"
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min left" if m else f"{h} h left"


def day_strip(feed: dict | None, now: datetime) -> dict | None:
    """Channel 3's day, two lines: the block in progress (or free time) and what's next + the temperature."""
    if not feed:
        return None
    events = []
    for e in feed.get("events") or []:
        if e.get("all_day"):
            continue
        try:
            events.append((datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]), e.get("title") or "Busy"))
        except (KeyError, TypeError, ValueError):
            continue
    events.sort(key=lambda x: x[0])
    cur = next((e for e in events if e[0] <= now < e[1]), None)
    nxt = next((e for e in events if e[0] > now and e[0].date() == now.date()), None)
    if cur:
        line1 = f"{cur[2]} · {_left(math.ceil((cur[1] - now).total_seconds() / 60))}"
    elif nxt:
        line1 = f"Free until {_hm(nxt[0])}"
    else:
        line1 = "Nothing else on the calendar today"
    parts = [f"Next {_hm(nxt[0])} {nxt[2]}"] if nxt else []
    w = feed.get("weather") or {}
    if w.get("temp") is not None:
        parts.append(f"{round(w['temp'])}° {w.get('sky') or ''}".strip())
    return {"line1": line1, "line2": " · ".join(parts)}


def tv_line(status: dict) -> str:
    if not status.get("channel_number"):
        return "📺 TV not responding"
    return f"📺 {status['channel_number']} · {status.get('title') or status.get('network_name') or ''}".rstrip(" ·")


_ENGLISH = {"eng", "en", ""}   # unlabeled tracks count as English


def _sub_name(track: dict) -> str:
    lang, title = (track.get("lang") or "").lower(), (track.get("title") or "").strip()
    if lang in ("eng", "en"):
        return "English" if not title or title.lower() == "english" else f"English ({title})"
    return title or (lang.upper() if lang else f"Track {track.get('id')}")


def next_subtitle(tracks: list[dict], visible: bool, sid) -> tuple[dict | None, str]:
    """SUBS walks the English (or unlabeled) subtitle tracks, then OFF, then back to the first one. Other
    languages are skipped (a single file can carry 29 tracks); a file with no English track offers its first one.

    tracks: mpv's track-list; visible: mpv's sub-visibility; sid: mpv's sid (a track id, or False/"no").
    Returns (mpv properties to set, label for the phone). OFF is sub-visibility=no, which mpv keeps across
    files, so subtitles stay off through breaks and the next episode until SUBS is pressed again."""
    subs = [t for t in tracks if t.get("type") == "sub"]
    if not subs:
        return None, "No subtitles on this"
    choices = [t for t in subs if (t.get("lang") or "").lower() in _ENGLISH] or subs[:1]
    ids = [t.get("id") for t in choices]
    if not visible or sid not in ids:     # off, nothing picked, or another language on -> first English track
        nxt = choices[0]
    elif ids.index(sid) == len(ids) - 1:
        return {"sub-visibility": False}, "SUBS: OFF"
    else:
        nxt = choices[ids.index(sid) + 1]
    return {"sid": nxt.get("id"), "sub-visibility": True}, f"SUBS: {_sub_name(nxt)}"


def _key(kind: str | None, file: str | None, spot: str | None, channel) -> str | None:
    if kind in ("episode", "standby") and file:
        return f"file:{file}"
    if kind == "commercial" and spot:
        return f"spot:{spot}"
    if kind in ("weather", "guide", "today", "web", "bump", "app") and channel is not None:
        return f"{kind}:{channel}"
    return None


def content_key(status: dict, slate: str | None) -> str | None:
    """What's on screen, as a key that matches earlier ratings of it (for the 👍/👎 halo). A standby slate
    rates the file underneath, so its key is that file's."""
    c = classify(status, None)
    return _key(c["kind"], status.get("file_path"), c["commercial_file"], status.get("channel_number"))


def record_key(rec: dict) -> str | None:
    kind = rec.get("kind")
    if kind == "standby":           # the slate covered whatever was playing: key it like that item
        kind = "commercial" if rec.get("commercial_file") else "episode"
    return _key(kind, rec.get("file"), rec.get("commercial_file"), rec.get("channel"))


def latest_thumbs(records) -> dict[str, str]:
    """key -> the most recent thumb ('up'/'down') given to it."""
    out = {}
    for rec in records:
        k = record_key(rec)
        if k and rec.get("thumb") in ("up", "down"):
            out[k] = rec["thumb"]
    return out
