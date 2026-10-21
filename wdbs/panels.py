"""Era side panels for 4:3 shows on Channel 10 (drawn by wdbs-bug.service).

Spec: docs/superpowers/specs/2026-09-28-era-panels-design.md.
Two 240x1080 panels per era (wdbs-graphics EraPanelLeft/Right-<era>) fill the black bars of a 4:3 picture on
the 1920x1080 OSD: overlay 39 at x=0, overlay 40 at x=1680. The right panel carries a live clock, painted here
with Pillow onto panel_right_<era>.png once a minute and written as premultiplied BGRA to tmpfs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .era import DEFAULT_ERA, ERAS

LOG = logging.getLogger("wdbs.panels")
PANEL_W, PANEL_H = 240, 1080
LEFT_X, RIGHT_X = 0, 1680
LEFT_ID, RIGHT_ID = 39, 40            # below the bug (41) and the standby slates (42)
MAX_ASPECT = 1.334                    # bars >= 240 px on 1920x1080: a panel never covers the picture
TIME_FONT, AMPM_FONT = "TitanOne-Regular.ttf", "BarlowCondensed-SemiBold.ttf"
RAW_BYTES = PANEL_W * PANEL_H * 4


@dataclass(frozen=True)
class PanelAsset:
    era: str
    left_raw: Path
    right_raw: Path
    right_png: Path
    clock: dict
    style: dict


def _raw_ok(path: Path) -> bool:
    from .bug import premultiplied_ok    # late import: bug imports this module
    data = path.read_bytes()
    if len(data) != RAW_BYTES:
        return False
    return min(data[3::4]) == 255 or premultiplied_ok(data)   # opaque panels are trivially premultiplied


def load_panels(assets_dir: Path) -> dict[str, PanelAsset]:
    """Every era with a complete, valid panel set."""
    out = {}
    d = Path(assets_dir)
    for era in ERAS:
        left, right, png, meta = (d / f"panel_left_{era}.bgra", d / f"panel_right_{era}.bgra",
                                  d / f"panel_right_{era}.png", d / f"panel_{era}.json")
        try:
            m = json.loads(meta.read_text())
            clock = {k: int(m["clock"][k]) for k in ("x", "y", "w", "h")}
            style = {"color": str(m["color"]), "shadow": str(m["shadow"]),
                     "shadow_dx": int(m["shadow_dx"]), "shadow_dy": int(m["shadow_dy"])}
            ok = png.exists() and _raw_ok(left) and _raw_ok(right)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("no usable panels for %s (%s)", era, exc)
            continue
        if not ok:
            LOG.warning("panels for %s are not 240x1080 premultiplied BGRA (+png); skipped", era)
            continue
        out[era] = PanelAsset(era, left, right, png, clock, style)
    return out


def pick_panels(panels: dict[str, PanelAsset], era: str) -> PanelAsset | None:
    return panels.get(era) or panels.get(DEFAULT_ERA)


def clock_text(when: dt.datetime) -> tuple[str, str]:
    return when.strftime("%I:%M").lstrip("0"), when.strftime("%p")


class ClockPainter:
    """Paints the time onto the right panel; returns None (and warns once) when fonts or Pillow are unusable.

    Permanent failures (missing fonts or Pillow) disable the clock for the service lifetime.
    Transient failures (bad PNG, write errors) are retried each minute, with per-era warning logging.
    """

    def __init__(self, fonts_dir: Path, out_path: Path):
        self.fonts_dir, self.out_path = Path(fonts_dir), Path(out_path)
        self._fonts: dict[tuple[str, int], object] = {}
        self._broken = False
        self._ready_checked = False
        self._warned: set[str] = set()

    def _font(self, name: str, size: int):
        from PIL import ImageFont
        key = (name, size)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(str(self.fonts_dir / name), size)
        return self._fonts[key]

    def _ready(self) -> bool:
        """Check fonts and Pillow availability once. Sets _broken if unavailable."""
        if self._ready_checked:
            return not self._broken
        self._ready_checked = True
        try:
            from PIL import Image, ImageDraw, ImageFont  # noqa: F401
            self._font(TIME_FONT, 64)
            self._font(AMPM_FONT, 34)
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("clock unavailable (%s); panels show without it", exc)
            self._broken = True
            return False

    def _fit(self, draw, text: str, name: str, max_w: int, start: int):
        size = start
        while size > 12:
            font = self._font(name, size)
            if draw.textlength(text, font=font) <= max_w:
                return font
            size -= 2
        return self._font(name, 12)

    def paint(self, panel: PanelAsset, when: dt.datetime) -> Path | None:
        if self._broken or not self._ready():
            return None
        try:
            from PIL import Image, ImageDraw
            img = Image.open(panel.right_png).convert("RGBA")
            draw = ImageDraw.Draw(img)
            c, s = panel.clock, panel.style
            hhmm, ampm = clock_text(when)
            inner = c["w"] - 24
            tfont = self._fit(draw, hhmm, TIME_FONT, inner, 64)
            afont = self._fit(draw, ampm, AMPM_FONT, inner, 34)
            tb, ab = draw.textbbox((0, 0), hhmm, font=tfont), draw.textbbox((0, 0), ampm, font=afont)
            th, ah, gap = tb[3] - tb[1], ab[3] - ab[1], 10
            top = c["y"] + (c["h"] - (th + gap + ah)) // 2
            for text, font, bbox, y in ((hhmm, tfont, tb, top), (ampm, afont, ab, top + th + gap)):
                x = c["x"] + (c["w"] - (bbox[2] - bbox[0])) // 2 - bbox[0]
                y -= bbox[1]
                draw.text((x + s["shadow_dx"], y + s["shadow_dy"]), text, font=font, fill=s["shadow"])
                draw.text((x, y), text, font=font, fill=s["color"])
            r, g, b, a = img.convert("RGBa").split()          # Pillow's premultiplied mode
            data = Image.merge("RGBA", (b, g, r, a)).tobytes()  # byte order B, G, R, A
            tmp = self.out_path.with_name(self.out_path.name + f".{os.getpid()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, self.out_path)
            return self.out_path
        except Exception as exc:  # noqa: BLE001
            if panel.era not in self._warned:
                LOG.warning("clock not painted for %s (%s); retrying next minute", panel.era, exc)
                self._warned.add(panel.era)
            return None
