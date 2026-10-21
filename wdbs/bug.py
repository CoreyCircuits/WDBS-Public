"""WDBS era corner bug on Channel 10 (runs on the TV host as wdbs-bug.service).

Spec: docs/superpowers/specs/2026-09-28-era-logos-design.md, section 3; screen dressing per
2026-09-28-era-panels-design.md. Also draws the era side panels (overlays 39/40) for 4:3 show segments.
One more client on FS42's mpv IPC socket (runtime/mpv.socket), the same pattern as wdbs.slate - no FS42
patch. Every TICK it reads mpv's path; while an episode of a rotation show plays, it shows that show's
era bug (wdbs-graphics EraBug-<era>: premultiplied BGRA, bottom-left) as OSD overlay 41, or - for a 4:3
show segment - that era's side panels (overlays 39/40, the right one carrying a live clock). Commercials,
station IDs and era IDs, clips, web entries, the standby image and an idle player get no bug or panels. The
standby slates (overlay 42) draw above it.

    python -m wdbs.bug [--dry-run]                     # --dry-run: log decisions, never touch the overlay
    python -m wdbs.bug --check-assets [--assets DIR]   # validate bugs, panels and fonts and exit
Disable: systemctl --user disable --now wdbs-bug.service (removes the panels, clock and bug on exit).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from . import fs42_adapter as fs
from . import panels as pn
from .config import Settings
from .era import DEFAULT_ERA, ERAS, tag_eras
from .programming import load_manifest
from .slate import MpvIpc

LOG = logging.getLogger("wdbs.bug")
TICK = 0.5
OVERLAY_ID = 41            # below the standby slates (wdbs.slate.OVERLAY_ID = 42)
MANIFEST_CHECK_S = 60.0
_UNKNOWN = object()        # forces the next decision to send an add or remove; removing a missing overlay is harmless


def era_on_screen(path: str | None, idle: bool, eras: dict[str, str], content_dir: str | None) -> str | None:
    """The era whose bug belongs on screen for this mpv path, or None for no bug."""
    if idle or not path or not content_dir:
        return None
    prefix = content_dir.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    tag = path[len(prefix):].split("/", 1)[0]
    return eras.get(tag)


@dataclass(frozen=True)
class BugAsset:
    era: str
    raw: Path
    w: int
    h: int
    x: int
    y: int


def premultiplied_ok(raw: bytes) -> bool:
    """Whole BGRA pixels, and no color component above its alpha (mpv's premultiplied rule)."""
    if len(raw) % 4:
        return False
    return all(max(raw[i], raw[i + 1], raw[i + 2]) <= raw[i + 3] for i in range(0, len(raw), 4))


def load_assets(assets_dir: Path) -> dict[str, BugAsset]:
    """Every era with a valid bug_<era>.json + bug_<era>.bgra pair."""
    out = {}
    for era in ERAS:
        meta, raw = Path(assets_dir) / f"bug_{era}.json", Path(assets_dir) / f"bug_{era}.bgra"
        try:
            m = json.loads(meta.read_text())
            w, h, x, y = (int(m[k]) for k in ("w", "h", "x", "y"))
            data = raw.read_bytes()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("no usable bug for %s (%s)", era, exc)
            continue
        if len(data) != w * h * 4 or not premultiplied_ok(data):
            LOG.warning("bug_%s.bgra is not %dx%d premultiplied BGRA; skipped", era, w, h)
            continue
        out[era] = BugAsset(era, raw, w, h, x, y)
    return out


def pick_asset(assets: dict[str, BugAsset], era: str) -> BugAsset | None:
    return assets.get(era) or assets.get(DEFAULT_ERA)


def decide(era: str | None, aspect, bugs: dict[str, BugAsset], panels: dict[str, pn.PanelAsset],
           clock_key: str) -> tuple:
    """What the screen should carry: ("none",), ("bug", era) or ("panels", era, clock_key)."""
    if not era:
        return ("none",)
    if panels and aspect is not None and float(aspect) <= pn.MAX_ASPECT:
        p = pn.pick_panels(panels, era)
        if p:
            return ("panels", p.era, clock_key)
    b = pick_asset(bugs, era)
    return ("bug", b.era) if b else ("none",)


def overlay_steps(want: tuple, shown) -> list[tuple]:
    """Overlay commands to move from `shown` to `want`; removes before adds. `shown` may be _UNKNOWN."""
    if want == shown:
        return []
    unknown = shown is _UNKNOWN
    was = None if unknown else shown[0]
    steps: list[tuple] = []
    if want[0] != "panels" and (unknown or was == "panels"):
        steps += [("remove", pn.LEFT_ID), ("remove", pn.RIGHT_ID)]
    if want[0] != "bug" and (unknown or was == "bug"):
        steps.append(("remove", OVERLAY_ID))
    if want[0] == "bug":
        steps.append(("add-bug", want[1]))
    elif want[0] == "panels":
        if unknown or was != "panels" or shown[1] != want[1]:
            steps.append(("add-left", want[1]))
        steps.append(("add-right", want[1], want[2]))
    return steps


def right_panel_file(panel: pn.PanelAsset, painter) -> Path:
    """The right panel with the current time painted on it, or the plain artwork if the clock is unavailable."""
    return painter.paint(panel, dt.datetime.now()) or panel.right_raw


HOLD_TICKS = 40  # wait up to 20 s (FS42's video_seek_timeout) for mpv's video-params/aspect before settling on the
                 # bug: a mid-episode tune-in seeks an XviD file and took 13 s to report it live (2026-09-28)
RETRY_TICKS = 20  # back off 10 s after a failed overlay step before trying another redraw, so a persistent
                  # failure (e.g. mpv socket wedged) doesn't repaint twice a second


class _NoClock:
    def paint(self, panel, when):
        return None


class Dresser:
    """What the screen carries, tick by tick. `command(*args) -> str` sends one mpv command and returns its
    error string ("success" when it worked); injected so the logic is testable without mpv."""

    def __init__(self, command, bugs, panels, painter=None, dry_run=False):
        self.command, self.bugs, self.panels, self.dry_run = command, bugs, panels, dry_run
        self.painter = painter or _NoClock()
        self.shown = _UNKNOWN
        self.hold = 0
        self.last_error = None
        self.retry = 0

    def reset(self):                      # after an IPC reconnect
        self.shown, self.hold, self.retry = _UNKNOWN, 0, 0

    def update(self, era, aspect, clock_key, loading=False):
        if era and self.panels and aspect is None and (loading or self.hold < HOLD_TICKS):
            if not loading:               # a stalled open (under the start slate) doesn't use up the hold
                self.hold += 1            # aspect not known yet: keep the screen as it is for now
            return
        self.hold = 0 if aspect is not None or not era else self.hold
        want = decide(era, aspect, self.bugs, self.panels, clock_key)
        if self.dry_run:
            if self.shown is _UNKNOWN or want[:2] != self.shown[:2]:
                LOG.info("would %s", _describe(want))
            self.shown = want
            return
        if self.retry > 0 and want != ("none",):
            self.retry -= 1                # backing off after a failure: skip this redraw attempt
            return
        self.retry = 0
        steps = overlay_steps(want, self.shown)
        if not steps:
            return
        errors = [e for e in (self._execute(s) for s in steps) if e != "success"]
        if errors:
            if errors[0] != self.last_error:
                LOG.warning("overlay command failed (%s); full redraw in 10 s", errors[0])
            self.last_error = errors[0]
            self.shown = _UNKNOWN         # a partial failure: forget the screen, full cleanup + re-add next tick
            self.retry = RETRY_TICKS      # ...but not every tick: back off before trying again
            return
        self.last_error = None
        if any(s[0] in ("add-bug", "add-left") for s in steps) or want == ("none",):
            LOG.info("%s (success)", _describe(want))
        self.shown = want

    def _execute(self, step) -> str:     # same commands as run()'s old execute(), via self.command
        kind = step[0]
        if kind == "remove":
            return self.command("overlay-remove", step[1])
        if kind == "add-bug":
            a = pick_asset(self.bugs, step[1])
            return self.command("overlay-add", OVERLAY_ID, a.x, a.y, str(a.raw), 0, "bgra", a.w, a.h, a.w * 4)
        p = pn.pick_panels(self.panels, step[1])
        if kind == "add-left":
            oid, x, raw = pn.LEFT_ID, pn.LEFT_X, p.left_raw
        else:
            oid, x, raw = pn.RIGHT_ID, pn.RIGHT_X, right_panel_file(p, self.painter)
        return self.command("overlay-add", oid, x, 0, str(raw), 0, "bgra", pn.PANEL_W, pn.PANEL_H, pn.PANEL_W * 4)


class ManifestWatch:
    """tag -> era from the rotation manifest; re-read when its mtime changes, keeping the last good map."""

    def __init__(self, path: Path, check_every: float = MANIFEST_CHECK_S):
        self.path, self.check_every = Path(path), check_every
        self.eras: dict[str, str] = {}
        self._mtime: float | None = None
        self._checked: float | None = None

    def get(self, now: float) -> dict[str, str]:
        if self._checked is not None and now - self._checked < self.check_every:
            return self.eras
        self._checked = now
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            LOG.warning("manifest unreadable (%s); keeping %d known show(s)", exc, len(self.eras))
            return self.eras
        if mtime != self._mtime:
            m = load_manifest(self.path)
            if m.error:
                LOG.warning("manifest unreadable (%s); keeping %d known show(s)", m.error, len(self.eras))
                return self.eras
            self.eras, self._mtime = tag_eras(m.programs), mtime
            LOG.info("eras: %s", ", ".join(f"{t}={e}" for t, e in sorted(self.eras.items())) or "none")
        return self.eras


def clear_overlays(command, dry_run: bool) -> None:
    """Remove the panel and bug overlays - unless this is a dry run: a dry run is only an observer on the
    live mpv socket, and its exit must never strip the live service's overlays."""
    if dry_run:
        return
    for oid in (pn.LEFT_ID, pn.RIGHT_ID, OVERLAY_ID):
        command("overlay-remove", oid)


def run(settings: Settings, channel: int, assets: dict[str, BugAsset], dry_run: bool = False,
        panels: dict[str, pn.PanelAsset] | None = None, painter=None):
    panels = panels or {}
    content_dir = fs.station_for_channel(settings, channel).content_dir
    ipc = MpvIpc(settings.fs42_root / "runtime" / "mpv.socket")
    manifest = ManifestWatch(settings.manifest)
    command = lambda *a: (ipc.command(*a).get("error") or "no response")  # noqa: E731
    dresser = Dresser(command, assets, panels, painter, dry_run)

    def stop(*_):  # never leave panels or a bug on screen when the service stops
        try:
            if ipc.s is None:
                ipc.connect()
            clear_overlays(command, dry_run)
        except (OSError, ConnectionError, ValueError):
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        try:
            if ipc.s is None:
                ipc.connect()
                dresser.reset()
                LOG.info("connected to %s (content dir %s, osd %sx%s, %d panel set(s))", ipc.sock_path, content_dir,
                         ipc.get("osd-width"), ipc.get("osd-height"), len(panels))
            era = era_on_screen(ipc.get("path"), bool(ipc.get("idle-active")),
                                manifest.get(time.monotonic()), content_dir)
            aspect = ipc.get("video-params/aspect") if era and panels else None
            loading = aspect is None and bool(era and panels) and ipc.get("time-pos") is None
            dresser.update(era, aspect, time.strftime("%H:%M"), loading)
            time.sleep(TICK)
        except (OSError, ConnectionError, ValueError) as exc:
            if ipc.s is not None:
                LOG.warning("mpv IPC lost (%s); retrying", exc)
            ipc.close()
            dresser.reset()
            time.sleep(2)


def _describe(want: tuple) -> str:
    if want[0] == "bug":
        return f"show {want[1]} bug"
    if want[0] == "panels":
        return f"show panels {want[1]}"
    return "hide"


def check_assets(assets_dir: Path) -> int:
    assets = load_assets(assets_dir)
    for era in ERAS:
        a = assets.get(era)
        print(f"{era}: " + (f"ok {a.w}x{a.h} at ({a.x},{a.y})" if a else "MISSING OR INVALID"))
    panels = pn.load_panels(assets_dir)
    for era in ERAS:
        print(f"panels {era}: " + ("ok" if era in panels else "MISSING OR INVALID"))
    fonts_dir = Path(assets_dir) / "fonts"
    fonts_ok = (fonts_dir / pn.TIME_FONT).exists() and (fonts_dir / pn.AMPM_FONT).exists()
    print("fonts: " + ("ok" if fonts_ok else "MISSING"))
    return 0 if set(assets) == set(ERAS) and set(panels) == set(ERAS) and fonts_ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wdbs bug")
    ap.add_argument("--fs42-root", default=os.environ.get("WDBS_FS42_ROOT", str(Path.home() / "FieldStation42")))
    ap.add_argument("--assets", default=None, help="dir with bug_<era>.bgra/.json (default <fs42>/wdbs_assets/era)")
    ap.add_argument("--manifest", default=None, help="rotation manifest (default: Settings / WDBS_MANIFEST)")
    ap.add_argument("--channel", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-assets", action="store_true", help="validate bugs, panels and fonts and exit")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    base = Settings()
    settings = Settings(fs42_root=Path(a.fs42_root), state_db=base.state_db,
                        manifest=Path(a.manifest) if a.manifest else base.manifest)
    assets_dir = Path(a.assets) if a.assets else settings.fs42_root / "wdbs_assets" / "era"
    if a.check_assets:
        return check_assets(assets_dir)
    assets = load_assets(assets_dir)
    if DEFAULT_ERA not in assets and not a.dry_run:
        raise SystemExit(f"no valid everyday ({DEFAULT_ERA}) bug in {assets_dir}; refusing to start")
    panels = pn.load_panels(assets_dir)
    painter = pn.ClockPainter(assets_dir / "fonts",
                              Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "wdbs-panel-right.bgra")
    run(settings, a.channel, assets, a.dry_run, panels, painter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
