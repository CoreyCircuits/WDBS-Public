"""WDBS volume meter (runs on the TV host as wdbs-volume.service). Decisions live in wdbs.volume_core.

Listens to PulseAudio (`pactl subscribe`) and, whenever the default sink's volume or mute state changes - from the
phone remote, the air-mouse, the keyboard or anything else - drops a cable-box style meter in from the top edge of
the screen for a couple of seconds. One small override-redirect, click-through window above mpv and the web windows,
re-raised every 500 ms while visible and unmapped when idle (the same technique as wdbs.tuner).

    python -m wdbs.volume                                          # the service
    python -m wdbs.volume --snapshot out.png --percent 65 [--muted] # one frame, offscreen (QT_QPA_PLATFORM=offscreen)
Disable: systemctl --user disable --now wdbs-volume (the volume buttons still work; only the meter goes away).
Position: WDBS_VOLUME_POS=top (default) | bottom.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from wdbs import volume_core as vc

LOG = logging.getLogger("wdbs.volume")
RAISE_MS = 500
FRAME_MS = 33
SAFE = 0.05             # action-safe margin on every side (same as the tuning banner)
HONEY, HONEY_HI, HONEY_LO, CEDAR, DUSK, DUSK_HI, WHITE = (
    "#DD9B51", "#F4C584", "#B9722F", "#302337", "#206289", "#3E90BF", "#F6F1E8")
ALERT = "#D8394A"
GUIDE_FONTS = Path(__file__).resolve().parents[1] / "fs42_overlay" / "static" / "wdbsguide" / "fonts"
PCTL_ENV = {**os.environ, "LC_ALL": "C"}        # "Mute: yes" must not be translated

# The meter is designed on a 1080p screen and scaled by screen height.
PILL_W, PILL_H = 760, 120
BLOCK_W = 150
BAR_X, BAR_W, BAR_H, GAP = 182, 420, 38, 5
NUM_X = 622


def _font(paths: list[Path], fallback: str) -> str:
    for path in paths:
        fid = QFontDatabase.addApplicationFont(str(path))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if fams:
            return fams[0]
        LOG.warning("font not loaded: %s", path)
    return fallback


def _px(family: str, size: float) -> QFont:
    f = QFont(family)
    f.setPixelSize(max(1, int(size)))
    return f


class Meter(QWidget):
    """The one window: the pill, sliding in from its screen edge and fading out. Unmapped when idle."""

    def __init__(self, position: str = "top"):
        super().__init__()
        self.setWindowTitle("WDBS-VOLUME")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.X11BypassWindowManagerHint
                            | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.position = position
        self.num_family = _font([GUIDE_FONTS / "archivo-black.woff2"], "Archivo Black")
        self.txt_family = _font([GUIDE_FONTS / "barlow-condensed-700.woff2"], "Barlow Condensed")
        self.core = vc.Meter()
        self.alpha, self.slide = 0.0, 0.0
        self.s = 1.0                                    # scale: screen height / 1080
        self.margin = 0
        self.place(QApplication.primaryScreen().geometry())

    def place(self, screen) -> None:
        """Size and position the window; it spans the pill plus the strip it slides through."""
        self.s = screen.height() / 1080
        self.margin = round(screen.height() * SAFE)
        w, h = round(PILL_W * self.s), round(PILL_H * self.s)
        strip = h + self.margin
        x = screen.x() + (screen.width() - w) // 2
        y = screen.y() if self.position == "top" else screen.y() + screen.height() - strip
        self.setGeometry(x, y, w, strip)

    def paint_pill(self, p: QPainter, reading: vc.Reading, alpha: float, slide: float) -> None:
        """Draw the pill into a window of the size place() made. Shared by the live window and --snapshot."""
        s = self.s
        pct, muted = reading
        w, h = PILL_W * s, PILL_H * s
        # The strip is margin + pill (top) or pill + margin (bottom); the pill slides in from its screen edge.
        off = (1.0 - slide) * (h + self.margin)
        top = self.margin - off if self.position == "top" else off
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setOpacity(alpha)
        p.translate(0, top)
        r = h * 0.1
        p.setPen(QColor(246, 241, 232, 60))
        p.setBrush(QColor(CEDAR))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)
        # speaker block
        blk = QRectF(0, 0, BLOCK_W * s, h)
        g = QLinearGradient(blk.topLeft(), blk.bottomLeft())
        g.setColorAt(0.0, QColor(DUSK_HI)); g.setColorAt(0.5, QColor(DUSK))
        g.setColorAt(0.51, QColor("#154663")); g.setColorAt(1.0, QColor("#1B557A"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(blk, r, r)
        self.paint_speaker(p, blk, pct, muted)
        # label
        label_f = _px(self.txt_family, 24 * s)
        label_f.setLetterSpacing(QFont.PercentageSpacing, 140)
        p.setFont(label_f)
        p.setPen(QColor(ALERT) if muted else QColor(246, 241, 232, 200))
        p.drawText(QRectF(BAR_X * s, 10 * s, BAR_W * s, 30 * s), Qt.AlignLeft | Qt.AlignVCenter,
                   "MUTED" if muted else "VOLUME")
        # segmented bar
        lit = vc.lit_segments(pct)
        p.setPen(Qt.NoPen)
        seg_w = (BAR_W - GAP * (vc.SEGMENTS - 1)) / vc.SEGMENTS
        by = 52 * s
        for i in range(vc.SEGMENTS):
            rect = QRectF((BAR_X + i * (seg_w + GAP)) * s, by, seg_w * s, BAR_H * s)
            if i < lit:
                if muted:
                    p.setBrush(QColor(246, 241, 232, 70))
                else:       # the 2000s gloss: a four-stop gradient with a hard split at 50/51 %
                    sg = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                    sg.setColorAt(0.0, QColor(HONEY_HI)); sg.setColorAt(0.5, QColor(HONEY))
                    sg.setColorAt(0.51, QColor(HONEY_LO)); sg.setColorAt(1.0, QColor("#A9672B"))
                    p.setBrush(sg)
            else:
                p.setBrush(QColor(246, 241, 232, 30))
            p.drawRoundedRect(rect, 3 * s, 3 * s)
        # number
        p.setFont(_px(self.num_family, 54 * s))
        p.setPen(QColor(246, 241, 232, 110) if muted else QColor(WHITE))
        p.drawText(QRectF(NUM_X * s, 0, (PILL_W - NUM_X - 14) * s, h), Qt.AlignRight | Qt.AlignVCenter, str(pct))
        p.restore()

    @staticmethod
    def paint_speaker(p: QPainter, box: QRectF, pct: int, muted: bool) -> None:
        """A speaker cone with 0-3 sound arcs for the level, or a red cross when muted (no icon set in this repo)."""
        u = box.height() / 120                          # the icon is drawn on a 120-high grid
        cx, cy = box.center().x() - 8 * u, box.center().y()
        body = QPainterPath()
        body.moveTo(cx - 26 * u, cy - 11 * u)
        body.lineTo(cx - 10 * u, cy - 11 * u)
        body.lineTo(cx + 8 * u, cy - 27 * u)
        body.lineTo(cx + 8 * u, cy + 27 * u)
        body.lineTo(cx - 10 * u, cy + 11 * u)
        body.lineTo(cx - 26 * u, cy + 11 * u)
        body.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(WHITE))
        p.drawPath(body)
        pen = QPen(QColor(WHITE), 5 * u)
        pen.setCapStyle(Qt.RoundCap)
        if muted:
            pen.setColor(QColor(ALERT))
            pen.setWidthF(7 * u)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            x0, y0 = cx + 18 * u, cy - 14 * u
            p.drawLine(QPointF(x0, y0), QPointF(x0 + 28 * u, y0 + 28 * u))
            p.drawLine(QPointF(x0 + 28 * u, y0), QPointF(x0, y0 + 28 * u))
            return
        waves = 0 if pct <= 0 else 1 if pct < 34 else 2 if pct < 67 else 3
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for n in range(waves):
            rad = (16 + n * 13) * u
            p.drawArc(QRectF(cx + 8 * u - rad, cy - rad, rad * 2, rad * 2), -45 * 16, 90 * 16)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if self.core.reading and self.alpha > 0:
            self.paint_pill(p, self.core.reading, self.alpha, self.slide)
        p.end()


class Bridge(QObject):
    reading = Signal(int, bool)


def query_reading() -> vc.Reading | None:
    try:
        vol = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], capture_output=True, text=True,
                             timeout=2, env=PCTL_ENV).stdout
        mute = subprocess.run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], capture_output=True, text=True,
                              timeout=2, env=PCTL_ENV).stdout
    except (OSError, subprocess.SubprocessError) as e:
        LOG.warning("pactl query failed: %s", e)
        return None
    return vc.parse_reading(vol, mute)


def watch(bridge: Bridge, stop: threading.Event) -> None:
    """Thread: baseline once, then a reading after every sink/server event. Reconnects if PulseAudio restarts."""
    while not stop.is_set():
        try:
            rd = query_reading()
            if rd:
                bridge.reading.emit(*rd)
            proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True, env=PCTL_ENV)
            for line in proc.stdout:
                if stop.is_set():
                    break
                if vc.is_sink_event(line):
                    rd = query_reading()
                    if rd:
                        bridge.reading.emit(*rd)
            proc.terminate()
        except OSError as e:
            LOG.warning("pactl subscribe failed: %s", e)
        stop.wait(3)


def snapshot(out: Path, percent: int, muted: bool, position: str) -> int:
    meter = Meter(position)
    screen = QApplication.primaryScreen().geometry()
    img = QImage(screen.width(), screen.height(), QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(34, 38, 44))                        # stand-in picture behind the meter
    p = QPainter(img)
    meter.s = screen.height() / 1080
    meter.margin = round(screen.height() * SAFE)
    w, strip = round(PILL_W * meter.s), round(PILL_H * meter.s) + meter.margin
    p.translate((screen.width() - w) // 2, 0 if position == "top" else screen.height() - strip)
    meter.resize(w, strip)
    meter.paint_pill(p, (percent, muted), 1.0, 1.0)
    p.end()
    return 0 if img.save(str(out)) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="WDBS volume meter")
    ap.add_argument("--snapshot", type=Path)
    ap.add_argument("--percent", type=int, default=65)
    ap.add_argument("--muted", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    position = os.environ.get("WDBS_VOLUME_POS", "top")
    app = QApplication(sys.argv[:1])
    if a.snapshot:
        return snapshot(a.snapshot, a.percent, a.muted, position)

    meter = Meter(position)
    bridge, stop = Bridge(), threading.Event()

    def on_reading(pct: int, muted: bool) -> None:
        if meter.core.observe((pct, muted), time.monotonic()):
            LOG.info("volume %d%%%s", pct, " muted" if muted else "")
            frame.start(FRAME_MS)
            raise_t.start(RAISE_MS)
            on_frame()

    def on_frame() -> None:
        meter.alpha, meter.slide = meter.core.state(time.monotonic())
        if meter.core.visible:
            if not meter.isVisible():
                meter.show()
            meter.update()
        else:
            frame.stop()
            raise_t.stop()
            meter.hide()

    frame, raise_t = QTimer(), QTimer()
    frame.timeout.connect(on_frame)
    raise_t.timeout.connect(meter.raise_)
    bridge.reading.connect(on_reading)
    threading.Thread(target=watch, args=(bridge, stop), daemon=True).start()

    def quit_(*_):
        stop.set()
        app.quit()

    signal.signal(signal.SIGTERM, quit_)
    signal.signal(signal.SIGINT, quit_)
    sig_pump = QTimer()                                 # lets Python signal handlers run inside the Qt loop
    sig_pump.timeout.connect(lambda: None)
    sig_pump.start(500)
    LOG.info("watching PulseAudio (position %s)", position)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
