"""Tuning transition - pure logic (spec docs/superpowers/specs/2026-09-28-tuning-transition-design.md).

What the cable-box banner says for a channel, which era backdrop goes behind it, when the picture counts as
moving, and the hold state machine. No Qt, no files, no FS42 imports: wdbs/tuner.py does the I/O.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from fs42_overlay.wdbs_remote_core import episode_info
from wdbs.era import DEFAULT_ERA, era_for

GUIDE_CHANNEL = 99
NAMES = {4: "WDBS WEATHER", 10: "WDBS SITCOMS", 90: "WDBS TEST", 99: "WDBS GUIDE"}     # 
TAGLINES = {3: "Your day, live", 4: "WeatherCast · Exampleville forecast, live", 9: "Interactive · Powered by Stremio",
            99: "What's on WDBS"}


@dataclass(frozen=True)
class Show:
    name: str
    era: str


@dataclass(frozen=True)
class Banner:
    channel: int
    network: str
    title: str
    detail: str = ""
    next: str = ""


@dataclass(frozen=True)
class Card:
    banner: Banner
    era: str


def show_index(programs: list[dict]) -> dict[str, Show]:
    """Rotation-manifest programs -> tag: Show(name, era)."""
    out = {}
    for p in programs or []:
        tag = (p.get("tag") or "").strip()
        if tag:
            out[tag] = Show(p.get("program") or tag, era_for(p.get("premiere_year"), p.get("era_override")))
    return out


def network_name(channel: int, station_name: str | None) -> str:
    if channel in NAMES:
        return NAMES[channel]
    name = re.sub(r"^TV_HOST\b", "WDBS", (station_name or "").strip())
    return name or f"CHANNEL {channel}"


def _hm(t) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d}"


def _feature_path(block) -> str | None:
    for p in block.plan_paths():
        if p.get("content_type") == "feature" and p.get("path"):
            return p["path"]
    return None


def _tag(path: str | None, content_dir: str | None) -> str | None:
    if not path or not content_dir:
        return None
    prefix = content_dir.rstrip("/") + "/"
    return path[len(prefix):].split("/", 1)[0] if path.startswith(prefix) else None


def _title(block, content_dir, shows) -> tuple[str, Show | None]:
    path = _feature_path(block)
    show = shows.get(_tag(path, content_dir))
    if show:
        return show.name, show
    raw = os.path.splitext(os.path.basename(path or block.title or ""))[0]
    raw = re.split(r"[ ._-]*[Ss]\d{1,2}[Ee]\d{1,3}", raw)[0]
    return re.sub(r"[._]+", " ", raw).strip(" -") or (block.title or ""), None


def card_for(channel: int, station_name: str | None, network_type: str | None, content_dir: str | None,
             block, next_block, shows: dict[str, Show]) -> Card:
    """The banner and backdrop era for tuning to `channel` now. `block`/`next_block`: the block on air and the
    one after it (anything with .start, .end, .title, .plan_paths()), or None."""
    network = network_name(channel, station_name)
    if network_type == "web" or block is None:
        return Card(Banner(channel, network, TAGLINES.get(channel, "")), DEFAULT_ERA)
    title, show = _title(block, content_dir, shows)
    ep, ep_title = episode_info(_feature_path(block) or "")
    detail = " · ".join(x for x in (ep, ep_title, f"{_hm(block.start)}–{_hm(block.end)}") if x)
    nxt = ""
    if next_block is not None:
        nxt = f"Next {_hm(next_block.start)} · {_title(next_block, content_dir, shows)[0]}"
    return Card(Banner(channel, network, title, detail, nxt), show.era if show else DEFAULT_ERA)


LINGER_S = 4.0          # banner stays this long after the picture
GUIDE_LINGER_S = 1.5    # shorter on the guide, so its own sting plays
INFO_S = 6.0
HANDOVER_S = 30.0       # a hold longer than this hands over to the standby slates
WEB_PAINT_S = 0.4       # a web page's load signal comes a little before its first paint
HOLD_FRESH_S = 35.0     # wdbs-slate trusts `holding` only while it is this fresh
MOVE_S = 0.3            # time-pos must advance this much for the picture to count as moving
STEP_MAX_S = 1.0        # a bigger change between two samples is a seek (FS42 seeks to the slot offset), not playback
_NOT_PICTURE = ("av://", "lavfi", ":autobump:", "http:", "https:")


class MoveWatch:
    """True once mpv's time-pos has advanced MOVE_S in steady steps on the same real file (not standby/black/web).
    Only small forward steps between consecutive samples count: FS42 plays a file at 0.0 and then seeks to the
    slot offset, and that jump (or a rewind, or a stall) starts the count again."""

    def __init__(self):
        self.path, self.last, self.moved = None, None, 0.0

    def update(self, path: str | None, time_pos: float | None) -> bool:
        if not path or path.startswith(_NOT_PICTURE) or path.endswith("standby.png") or time_pos is None:
            self.path, self.last, self.moved = None, None, 0.0
            return False
        if path != self.path or self.last is None:
            self.path, self.last, self.moved = path, time_pos, 0.0
            return False
        step, self.last = time_pos - self.last, time_pos
        self.moved = self.moved + step if 0 < step <= STEP_MAX_S else 0.0
        return self.moved >= MOVE_S


def watch_step(watch: MoveWatch, status_channel: int | None, target: int, path: str | None,
               time_pos: float | None) -> bool:
    """One picture-watch sample during a tune. While FS42's status still names another channel, mpv is playing the
    old channel's file, so its progress is thrown away. An unknown status (None) never blocks."""
    if status_channel is not None and status_channel != target:
        watch.path, watch.last, watch.moved = None, None, 0.0
        return False
    return watch.update(path, time_pos)


@dataclass(frozen=True)
class Display:
    backdrop: str | None
    banner: Banner | None
    holding: bool


def app_card(channel: int, station_name: str | None, status) -> Card:
    """INFO / tune card for a remote-app channel (Ch 9): the title the app is playing (FS42 status title, set by
    fs42/wdbs_remote_app.py from the app host's /nowplaying), else the channel tagline. Owner 9/29."""
    base = card_for(channel, station_name, "executable", None, None, None, {})
    title = getattr(status, "title", None) if status is not None and getattr(status, "channel", None) == channel else None
    if not title or title.strip().upper() == (station_name or "").strip().upper():
        return base
    return Card(Banner(channel, base.banner.network, title, "WDBS Stream · Interactive"), base.era)


def app_ready_url(channel: int) -> str:
    """A remote-app channel (station network_type "executable", Channel Router) signals its picture - or its WDBS
    unavailable slate - through wdbs_web_ready with this url (fs42/wdbs_router_core.ready_url)."""
    return f"wdbs-app:{channel}"


def hold_url(channel: int, network_type: str | None, web_url: str | None) -> str | None:
    """What load signal ends the tuning hold: the page for a web station, the app signal for a remote-app
    (executable) station, None (mpv picture watch) otherwise."""
    if network_type == "web":
        return web_url
    if network_type == "executable":
        return app_ready_url(channel)
    return None


def _same_page(a: str | None, b: str | None) -> bool:
    """FS42 reports the station's web_url exactly; anything else (a WeatherStar prewarm) is another page."""
    return bool(a) and a == b


class Tuner:
    """idle -> tuning (backdrop + banner, holding) -> playing (banner) -> idle; info = banner while idle."""

    def __init__(self):
        self.mode, self.card, self.since, self.until, self.web_url, self.ready_at = "idle", None, 0.0, 0.0, None, None
        self.expecting_web, self.page_at = False, None

    def tune(self, now: float, card: Card, web_url: str | None) -> None:
        self.mode, self.card, self.since, self.web_url, self.ready_at = "tuning", card, now, web_url, None
        self.expecting_web, self.page_at = False, None

    def expect_web(self, now: float) -> None:
        """A standard channel turned out to be playing a web entry (Ch 10's weather break): its page load is the
        picture. A page that already loaded since the tune counts."""
        if self.mode == "tuning" and not self.web_url and not self.expecting_web:
            self.expecting_web = True
            if self.page_at is not None:
                self.ready_at = self.page_at + WEB_PAINT_S

    def update_card(self, card: Card) -> None:
        if self.card is not None and self.mode != "idle" and card.banner.channel == self.card.banner.channel:
            self.card = card

    def picture(self, now: float) -> None:
        if self.mode == "tuning" and not self.web_url:
            self._playing(now)

    def web_ready(self, now: float, url: str) -> None:
        if self.mode != "tuning":
            return
        if self.web_url:
            if _same_page(self.web_url, url):
                self.ready_at = now + WEB_PAINT_S
        elif self.expecting_web:
            self.ready_at = now + WEB_PAINT_S
        else:
            self.page_at = now                      # maybe the web entry this tune lands in; expect_web decides

    def info(self, now: float, card: Card) -> None:
        if self.mode in ("idle", "info", "playing"):
            self.mode, self.card, self.until = "info", card, now + INFO_S

    def _playing(self, now: float) -> None:
        linger = GUIDE_LINGER_S if self.card.banner.channel == GUIDE_CHANNEL else LINGER_S
        self.mode, self.until = "playing", now + linger

    def tick(self, now: float) -> Display:
        if self.mode == "tuning":
            if self.ready_at is not None and now >= self.ready_at:
                self._playing(self.ready_at)
            elif now - self.since >= HANDOVER_S:
                self.mode = "idle"
        if self.mode in ("playing", "info") and now >= self.until:
            self.mode = "idle"
        if self.mode == "tuning":
            return Display(self.card.era, self.card.banner, True)
        if self.mode in ("playing", "info"):
            return Display(None, self.card.banner, False)
        return Display(None, None, False)


TUNER_STATE = Path(os.environ.get("WDBS_TUNER_STATE", str(Path.home() / ".local/state/wdbs/tuner.json")))


def holding_now(state: dict | None, now_wall: float) -> bool:
    """wdbs-slate: is the tuning card covering the screen right now? Stale or unreadable = no."""
    if not isinstance(state, dict):
        return False
    try:
        return bool(state.get("holding")) and now_wall - float(state["since"]) < HOLD_FRESH_S
    except (KeyError, TypeError, ValueError):
        return False
