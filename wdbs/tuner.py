"""WDBS tuning transition (runs on the TV host as wdbs-tuner.service). Spec: 2026-09-28-tuning-transition-design.md

Watches FS42's runtime/ for wdbs_tune.json (a channel change was decided), wdbs_web_ready (a web page loaded)
and wdbs_info.json (the remote's INFO). Shows a cable-box banner over an era backdrop in one override-redirect,
click-through window above mpv and the web windows, re-raised every 500 ms while visible. Decisions live in
wdbs.tuner_core; this module only does I/O and painting.

    python -m wdbs.tuner [--dry-run]                      # --dry-run: never show the window, log the timeline
    python -m wdbs.tuner --snapshot out.png --channel 10  # render one frame offscreen (QT_QPA_PLATFORM=offscreen)
Disable: systemctl --user disable --now wdbs-tuner (tunes look exactly as before; FS42's files are ignored).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from wdbs import fs42_adapter as fa
from wdbs import tuner_core as tc
from wdbs.config import Settings
from wdbs.programming import load_manifest
from wdbs.slate import MpvIpc

LOG = logging.getLogger("wdbs.tuner")
RAISE_MS = 500          # re-raise while visible; while tuning, every poll (FS42's web window raises itself)
POLL_MS = 100
FRAME_MS = 33
IPC_TIMEOUT_S = 0.2     # mpv IPC runs on the UI thread: a hiccup may cost one poll, never a frozen banner
FADE_IN_S = 0.1         # the backdrop comes up (near) instantly on a tune ...
FADE_S = 0.3            # ... fades out over 300 ms, and cross-fades over 300 ms when the era changes under it
SLIDE_S = 0.25
STATIONS_EVERY_S = 300
DB_TIMEOUT_S = 0.5      # a busy FS42 DB costs the banner its title, never the tune
SAFE = 0.05             # action-safe margin on every side
HONEY, CEDAR, DUSK, DUSK_HI, WHITE = "#DD9B51", "#302337", "#206289", "#3E90BF", "#F6F1E8"
GUIDE_FONTS = Path(__file__).resolve().parents[1] / "fs42_overlay" / "static" / "wdbsguide" / "fonts"


def assets_dir(settings: Settings) -> Path:
    """Backdrops + fonts; WDBS_TUNER_ASSETS points a snapshot at a scratch copy."""
    return Path(os.environ.get("WDBS_TUNER_ASSETS") or settings.fs42_root / "wdbs_assets" / "era")


def read_json(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


class Lookup:
    """What's on a channel now, from FS42's DB (read-only) and the rotation manifest; stations cached 5 min."""

    def __init__(self, settings: Settings):
        self.s, self._stations, self._at, self._shows, self._mtime = settings, {}, 0.0, {}, None
        self._lock = threading.Lock()          # the caches are shared by the lookup threads

    def station(self, channel: int):
        if time.monotonic() - self._at > STATIONS_EVERY_S or channel not in self._stations:
            try:
                self._stations = {st.channel: st for st in fa.load_stations(self.s)}
                self._at = time.monotonic()
            except Exception:
                LOG.exception("stations unreadable")
        return self._stations.get(channel)

    def shows(self) -> dict:
        try:
            mtime = self.s.manifest.stat().st_mtime
        except OSError:
            return self._shows
        if mtime != self._mtime:
            m = load_manifest(self.s.manifest)
            if m.error is None:
                self._shows, self._mtime = tc.show_index(m.programs), mtime
        return self._shows

    def card(self, channel: int, network: str | None = None) -> tc.Card:
        with self._lock:
            return self._card(channel, network)

    def _card(self, channel: int, network: str | None) -> tc.Card:
        st = self.station(channel)
        name = st.network_name if st else network
        ntype = st.network_type if st else None
        if ntype == "executable":                              # remote-app channel: show what the app is playing
            return tc.app_card(channel, name, fa.read_status(self.s))
        if st is None or ntype == "web":                        # no schedule
            return tc.card_for(channel, name, ntype, None, None, None, {})
        now = dt.datetime.now()
        try:
            with fa.connect_ro(self.s.fs42_db, timeout=DB_TIMEOUT_S) as conn:
                blocks = fa.get_blocks(conn, st.network_name, now - dt.timedelta(hours=1), now + dt.timedelta(hours=3))
        except Exception:
            LOG.exception("schedule unreadable")
            blocks = []
        cur = fa.block_at(blocks, now)
        nxt = next((b for b in blocks if cur and b.start >= cur.end), None)
        return tc.card_for(channel, st.network_name, st.network_type, st.content_dir, cur, nxt, self.shows())


def _font(paths: list[Path], fallback: str) -> str:
    """The family of the first font file Qt loads, else `fallback` (Qt then picks a system face)."""
    for path in paths:
        fid = QFontDatabase.addApplicationFont(str(path))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if fams:
            return fams[0]
        LOG.warning("font not loaded: %s", path)
    return fallback


def _px(family: str, size: float) -> QFont:
    f = QFont(family)
    f.setPixelSize(max(1, int(size)))     # pixels, not points: the same look whatever DPI X reports
    return f


class Overlay(QWidget):
    """The one window: era backdrop (fading, cross-fading between eras) + banner (sliding). Unmapped when idle."""

    def __init__(self, assets: Path):
        super().__init__()
        self.setWindowTitle("WDBS-TUNER")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.X11BypassWindowManagerHint
                            | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(QApplication.primaryScreen().geometry())
        self.assets, self._pix, self._missing = assets, {}, set()
        fonts = assets / "fonts"
        self.num_family = _font([GUIDE_FONTS / "archivo-black.woff2", fonts / "TitanOne-Regular.ttf"], "Archivo Black")
        self.txt_family = _font([fonts / "BarlowCondensed-SemiBold.ttf"], "Barlow Condensed")
        self.display = tc.Display(None, None, False)
        self.backdrop_alpha, self.banner_pos = 0.0, 0.0          # 0..1 each
        self.era, self.prev_era, self.blend = None, None, 1.0    # blend: 0 = prev_era .. 1 = era

    def backdrop(self, era: str | None) -> QPixmap | None:
        """The era's backdrop; a failed load is not cached (retried next paint) and warned about once."""
        if not era:
            return None
        if era not in self._pix:
            path = self.assets / f"tuning_{era}.png"
            p = QPixmap(str(path))
            if p.isNull():
                if era not in self._missing:
                    self._missing.add(era)
                    LOG.warning("backdrop not loaded: %s", path)
                return None
            self._pix[era] = p
        return self._pix[era]

    @property
    def visible(self) -> bool:
        return self.backdrop_alpha > 0 or self.banner_pos > 0

    def step(self, target: tc.Display, dt_s: float) -> bool:
        """Advance the fade/slide/cross-fade toward `target`; True if the picture changed (repaint needed)."""
        before = (self.display, self.era, self.prev_era, self.blend, self.backdrop_alpha, self.banner_pos)
        if target.backdrop or target.banner:
            self.display = target
        if target.backdrop and target.backdrop != self.era:
            # Surfing between eras under a fully shown backdrop: cross-fade. Still fading in (a tune's first card
            # turning into the real era a few ms later): just switch.
            showing = self.era is not None and self.backdrop_alpha >= 1.0
            self.prev_era, self.blend = (self.era, 0.0) if showing else (None, 1.0)
            self.era = target.backdrop
            self._pix = {k: v for k, v in self._pix.items() if k in (self.era, self.prev_era)}
        self.blend = min(1.0, self.blend + dt_s / FADE_S)
        if self.blend >= 1.0 and self.prev_era is not None:
            if self.prev_era != self.era:
                self._pix.pop(self.prev_era, None)                 # only the current era stays in memory
            self.prev_era = None
        want_b = 1.0 if target.backdrop else 0.0
        want_s = 1.0 if target.banner else 0.0
        self.backdrop_alpha += max(-dt_s / FADE_S, min(dt_s / FADE_IN_S, want_b - self.backdrop_alpha))
        self.banner_pos += max(-dt_s / SLIDE_S, min(dt_s / SLIDE_S, want_s - self.banner_pos))
        if not (target.backdrop or target.banner) and self.backdrop_alpha <= 0 and self.banner_pos <= 0:
            self.display = target
        return before != (self.display, self.era, self.prev_era, self.blend, self.backdrop_alpha, self.banner_pos)

    def paint_backdrop(self, p: QPainter, w: int, h: int) -> None:
        if self.backdrop_alpha <= 0:
            return
        full = QRectF(0, 0, w, h)
        prev = self.backdrop(self.prev_era) if self.prev_era and self.blend < 1.0 else None
        cur = self.backdrop(self.era)
        # The backdrops are opaque, so the old one at full strength with the new one over it at `blend`
        # is the true mix (1-blend)*old + blend*new: no dip to the picture underneath mid-cross-fade.
        for pix, weight in ((prev, 1.0), (cur, self.blend if prev else 1.0)):
            if pix and weight > 0:
                p.setOpacity(self.backdrop_alpha * weight)
                p.drawPixmap(full, pix, QRectF(pix.rect()))
        p.setOpacity(1.0)

    def paint_into(self, p: QPainter, w: int, h: int) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        self.paint_backdrop(p, w, h)
        b = self.display.banner
        if not b or self.banner_pos <= 0:
            return
        bh = h * 0.18
        bottom = h * (1 - SAFE)
        top = bottom - bh + (1.0 - self.banner_pos) * (bh + h * SAFE)
        x0, bw = w * SAFE, w * (1 - 2 * SAFE)
        band = QRectF(x0, top, bw, bh)
        p.setPen(QColor(246, 241, 232, 60))
        p.setBrush(QColor(CEDAR))
        p.drawRoundedRect(band, bh * 0.1, bh * 0.1)
        blk = QRectF(x0, top, bw * 0.24, bh)
        g = QLinearGradient(blk.topLeft(), blk.bottomLeft())
        g.setColorAt(0.0, QColor(DUSK_HI)); g.setColorAt(0.5, QColor(DUSK))
        g.setColorAt(0.51, QColor("#154663")); g.setColorAt(1.0, QColor("#1B557A"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(blk, bh * 0.1, bh * 0.1)
        p.setPen(QColor(WHITE))
        p.setFont(_px(self.num_family, bh * 0.46))
        p.drawText(QRectF(blk.x(), blk.y() + bh * 0.06, blk.width(), bh * 0.6), Qt.AlignHCenter | Qt.AlignVCenter,
                   str(b.channel))
        f = _px(self.txt_family, bh * 0.15)
        f.setLetterSpacing(QFont.PercentageSpacing, 118)
        p.setFont(f)
        inner = blk.width() * 0.9
        p.drawText(QRectF(blk.x(), blk.y() + bh * 0.66, blk.width(), bh * 0.25), Qt.AlignHCenter | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(b.network, Qt.ElideRight, int(inner)))
        tx = blk.right() + bw * 0.03
        tw = band.right() - tx - bw * 0.03
        lines = [(text, size, color) for text, size, color in (
            (b.title or b.network, 0.29, QColor(WHITE)), (b.detail, 0.17, QColor(HONEY)),
            (b.next, 0.16, QColor(185, 174, 191))) if text]
        y = top + (bh - sum(bh * size * 1.3 for _, size, _ in lines)) / 2     # the lines centred as a group
        for text, size, color in lines:
            p.setPen(color)
            p.setFont(_px(self.txt_family, bh * size))
            rect = QRectF(tx, y, tw, bh * size * 1.3)
            p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(text, Qt.ElideRight, int(tw)))
            y += rect.height()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        self.paint_into(p, self.width(), self.height())
        p.end()


def rss_mb() -> str:
    """This process's resident memory (BUG-001 budget: 80 MB), from /proc."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return f"{int(line.split()[1]) / 1024:.1f} MB"
    except (OSError, ValueError, IndexError):
        pass
    return "?"


class Bridge(QObject):
    """Hands cards from lookup threads to the UI thread: queued, so the slots run where Bridge lives."""
    filled = Signal(object)
    info = Signal(object)

    def __init__(self, on_filled, on_info):
        super().__init__()
        self._on_filled, self._on_info = on_filled, on_info
        self.filled.connect(self._filled, Qt.QueuedConnection)
        self.info.connect(self._info, Qt.QueuedConnection)

    @Slot(object)
    def _filled(self, card):
        self._on_filled(card)

    @Slot(object)
    def _info(self, card):
        self._on_info(card)


class Service:
    def __init__(self, settings: Settings, dry_run: bool):
        self.s, self.dry = settings, dry_run
        self.runtime = settings.fs42_root / "runtime"
        self.lookup = Lookup(settings)
        self.tuner = tc.Tuner()
        self.move = tc.MoveWatch()
        self.ipc = MpvIpc(self.runtime / "mpv.socket")
        self.overlay = Overlay(assets_dir(settings))
        self.bridge = Bridge(self.tuner.update_card, self.on_info)
        self.seen = {name: (read_json(self.runtime / name) or {}).get("at", 0)
                     for name in ("wdbs_tune.json", "wdbs_web_ready", "wdbs_info.json")}
        self.holding = None
        self.visible, self.channel, self.shown_at = False, None, 0.0
        self.watch = QFileSystemWatcher([str(self.runtime)])
        self.watch.directoryChanged.connect(lambda _: self.events())
        self.frame = QTimer(); self.frame.timeout.connect(self.on_frame); self.frame.start(FRAME_MS)
        self.poll = QTimer(); self.poll.timeout.connect(self.on_poll); self.poll.start(POLL_MS)
        self.raiser = QTimer(); self.raiser.timeout.connect(self.on_raise); self.raiser.start(RAISE_MS)
        self._last = time.monotonic()
        self.publish(False, None)

    def _new(self, name: str) -> dict | None:
        obj = read_json(self.runtime / name)
        try:
            at = float(obj.get("at", 0)) if obj else 0.0
        except (TypeError, ValueError):
            return None
        if at > self.seen[name]:
            self.seen[name] = at
            return obj
        return None

    @staticmethod
    def _background(fn, *args) -> None:
        def run():
            try:
                fn(*args)
            except Exception:
                LOG.exception("lookup failed")
        threading.Thread(target=run, daemon=True).start()

    def fill_in(self, channel: int, network: str | None) -> None:
        """Lookup thread: the schedule card for a tune; the UI thread applies it if that channel is still up."""
        started = time.monotonic()
        card = self.lookup.card(channel, network)
        LOG.info("card %s: %s, era %s (%.0f ms)", channel, card.banner.title or card.banner.network, card.era,
                 (time.monotonic() - started) * 1000)
        self.bridge.filled.emit(card)

    def info_lookup(self) -> None:
        """Lookup thread: INFO's card for whatever FS42 says is on."""
        status = fa.read_status(self.s)
        ch = getattr(status, "channel", None) if status else None
        if ch is not None:
            self.bridge.info.emit(self.lookup.card(int(ch)))

    def on_info(self, card: tc.Card) -> None:
        self.tuner.info(time.monotonic(), card)
        LOG.info("info %s", card.banner.channel)

    def events(self) -> None:
        now = time.monotonic()
        tune = self._new("wdbs_tune.json")
        if tune and tune.get("channel") is not None:
            ch, ntype, network = int(tune["channel"]), tune.get("type"), tune.get("network")
            web = tc.hold_url(ch, ntype, tune.get("web_url"))
            self.tuner.tune(now, tc.card_for(ch, network, ntype, None, None, None, {}), web)
            self.move = tc.MoveWatch()
            LOG.info("tune %s%s", ch, f" waits for {web}" if web else "")
            self.on_frame()                 # number + name on screen now; the schedule fills in when it answers
            if not web:
                self._background(self.fill_in, ch, network)
        ready = self._new("wdbs_web_ready")
        if ready:
            self.tuner.web_ready(now, ready.get("url", ""))
        if self._new("wdbs_info.json"):
            self._background(self.info_lookup)

    def on_poll(self) -> None:
        if self.tuner.mode != "tuning":
            return
        if not self.dry and self.overlay.isVisible():
            self.overlay.raise_()           # FS42's web window raises itself ~1.5 s in: stay on top while tuning
        if self.tuner.web_url:
            return
        target = self.tuner.card.banner.channel
        status = fa.read_status(self.s)
        if status is not None and status.channel == target and not self.tuner.expecting_web and (
                status.content_type == "web" or (status.file_path or "").startswith(":autobump:")):
            self.tuner.expect_web(time.monotonic())          # Ch 10 weather break: its page is the picture
            LOG.info("ch %s is playing a web entry: waiting for its page", target)
        try:
            if self.ipc.s is None:
                self.ipc.connect()
                self.ipc.s.settimeout(IPC_TIMEOUT_S)
            moving = tc.watch_step(self.move, status.channel if status else None, target,
                                   self.ipc.get("path"), self.ipc.get("time-pos"))
        except (OSError, ConnectionError, ValueError):
            self.ipc.close()
            return
        if moving:
            self.tuner.picture(time.monotonic())
            LOG.info("picture moving after %.2fs", time.monotonic() - self.tuner.since)

    def on_frame(self) -> None:
        now = time.monotonic()
        d = self.tuner.tick(now)
        if d.holding != self.holding:
            self.publish(d.holding, d.banner.channel if d.banner else None)
            LOG.info("holding %s", d.holding)
        if d.banner:
            self.channel = d.banner.channel
        changed = self.overlay.step(d, now - self._last)
        self._last = now
        visible = self.overlay.visible
        if visible != self.visible:
            self.timeline(visible, now)
        if self.dry:
            return
        if visible and not self.overlay.isVisible():
            self.overlay.show()
            self.overlay.raise_()
            self.overlay.update()
        elif not visible and self.overlay.isVisible():
            self.overlay.hide()
        elif visible and changed:
            self.overlay.update()           # a static card costs no repaints

    def timeline(self, visible: bool, now: float) -> None:
        self.visible = visible
        verb = ("would " if self.dry else "") + ("show" if visible else "hide")
        if visible:
            self.shown_at = now
            LOG.info("%s ch %s for %s, %.2fs after the tune", verb, self.channel, self.tuner.mode,
                     now - self.tuner.since)
        else:
            LOG.info("%s ch %s after %.2fs up (%.2fs since the tune), RSS %s", verb, self.channel,
                     now - self.shown_at, now - self.tuner.since, rss_mb())

    def on_raise(self) -> None:
        if not self.dry and self.overlay.isVisible():
            self.overlay.raise_()

    def publish(self, holding: bool, channel) -> None:
        """~/.local/state/wdbs/tuner.json for wdbs-slate; never written in a dry run (the slate must not obey it)."""
        self.holding = holding
        if self.dry:
            return
        try:
            tc.TUNER_STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = tc.TUNER_STATE.with_name(tc.TUNER_STATE.name + ".tmp")
            tmp.write_text(json.dumps({"holding": holding, "channel": channel, "since": time.time()}))
            os.replace(tmp, tc.TUNER_STATE)
        except OSError as exc:
            LOG.warning("tuner state not written: %s", exc)


def render(ov: Overlay, out: Path) -> bool:
    img = QImage(1920, 1080, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(20, 20, 20))
    p = QPainter(img)
    ov.paint_into(p, 1920, 1080)
    p.end()
    return img.save(str(out))


def snapshot(settings: Settings, out: Path, channel: int, info: bool) -> int:
    """Render one frame (tuning card, or INFO banner with --info) to a PNG, offscreen."""
    ov = Overlay(assets_dir(settings))
    card = Lookup(settings).card(channel)
    LOG.info("fonts: number %r, text %r; card %s", ov.num_family, ov.txt_family, card)
    ov.step(tc.Display(None if info else card.era, card.banner, not info), 10.0)
    return 0 if render(ov, out) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="WDBS tuning transition")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--snapshot", type=Path)
    ap.add_argument("--channel", type=int, default=10)
    ap.add_argument("--info", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    app = QApplication(sys.argv[:1])
    settings = Settings()
    if a.snapshot:
        return snapshot(settings, a.snapshot, a.channel, a.info)
    svc = Service(settings, a.dry_run)

    def stop(*_):
        # Python runs this between Qt events (the 33 ms frame timer hands it control): never leave "holding".
        LOG.info("stopping")
        svc.publish(False, None)
        app.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("wdbs-tuner started%s", " (dry run: the window never shows)" if a.dry_run else "")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
