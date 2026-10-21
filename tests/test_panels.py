import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from wdbs.bug import premultiplied_ok
from wdbs.panels import (PANEL_H, PANEL_W, ClockPainter, clock_text, load_panels, pick_panels)

GRAPHICS_FONTS = Path(__file__).resolve().parents[2] / "wdbs-graphics" / "assets" / "fonts"
CLOCK = {"x": 16, "y": 452, "w": 208, "h": 176}
STYLE = {"color": "#F6F1E8", "shadow": "#302337", "shadow_dx": 4, "shadow_dy": 4}


def write_panels(d: Path, era: str, left_ok=True, json_ok=True, color=(48, 35, 55)):
    img = Image.new("RGBA", (PANEL_W, PANEL_H), color + (255,))
    img.save(d / f"panel_right_{era}.png")
    raw = img.tobytes("raw", "BGRA")
    (d / f"panel_right_{era}.bgra").write_bytes(raw)
    (d / f"panel_left_{era}.bgra").write_bytes(raw if left_ok else raw[:-4])
    meta = {"era": era, "w": PANEL_W, "h": PANEL_H, "clock": CLOCK, **STYLE}
    (d / f"panel_{era}.json").write_text(json.dumps(meta) if json_ok else "{nope")


class PanelAssetTests(unittest.TestCase):
    def test_loads_complete_sets_and_skips_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_panels(d, "70s")
            write_panels(d, "00s")
            write_panels(d, "80s", left_ok=False)
            write_panels(d, "90s", json_ok=False)
            panels = load_panels(d)
            self.assertEqual(sorted(panels), ["00s", "70s"])
            p = panels["70s"]
            self.assertEqual((p.clock, p.style["color"]), (CLOCK, "#F6F1E8"))

    def test_missing_era_falls_back_to_everyday_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_panels(d, "00s")
            panels = load_panels(d)
            self.assertEqual(pick_panels(panels, "80s").era, "00s")
            self.assertIsNone(pick_panels({}, "80s"))


class ClockTests(unittest.TestCase):
    def test_clock_text(self):
        at = lambda h, m: dt.datetime(2026, 9, 28, h, m)
        self.assertEqual(clock_text(at(15, 7)), ("3:07", "PM"))
        self.assertEqual(clock_text(at(12, 0)), ("12:00", "PM"))
        self.assertEqual(clock_text(at(0, 5)), ("12:05", "AM"))
        self.assertEqual(clock_text(at(9, 41)), ("9:41", "AM"))

    @unittest.skipUnless((GRAPHICS_FONTS / "TitanOne-Regular.ttf").exists(), "fonts from wdbs-graphics not present")
    def test_painter_draws_inside_the_clock_box_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_panels(d, "70s")
            panel = load_panels(d)["70s"]
            out = d / "right.bgra"
            path = ClockPainter(GRAPHICS_FONTS, out).paint(panel, dt.datetime(2026, 9, 28, 15, 7))
            self.assertEqual(path, out)
            data = out.read_bytes()
            base = (d / "panel_right_70s.bgra").read_bytes()
            self.assertEqual(len(data), PANEL_W * PANEL_H * 4)
            self.assertTrue(premultiplied_ok(data))
            changed = {i // 4 for i in range(0, len(data), 4) if data[i:i + 4] != base[i:i + 4]}
            self.assertTrue(changed)
            for px in changed:
                x, y = px % PANEL_W, px // PANEL_W
                self.assertTrue(CLOCK["x"] <= x < CLOCK["x"] + CLOCK["w"] and CLOCK["y"] <= y < CLOCK["y"] + CLOCK["h"], (x, y))

    def test_painter_without_fonts_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_panels(d, "70s")
            panel = load_panels(d)["70s"]
            painter = ClockPainter(d / "no-fonts", d / "right.bgra")
            with self.assertLogs("wdbs.panels", level="WARNING") as logs:
                self.assertIsNone(painter.paint(panel, dt.datetime(2026, 9, 28, 15, 7)))
                self.assertIsNone(painter.paint(panel, dt.datetime(2026, 9, 28, 15, 8)))
            self.assertEqual(len(logs.output), 1)       # logged once, not every minute

    @unittest.skipUnless((GRAPHICS_FONTS / "TitanOne-Regular.ttf").exists(), "fonts from wdbs-graphics not present")
    def test_bad_panel_png_does_not_stop_the_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_panels(d, "70s")
            write_panels(d, "80s")
            # Corrupt the 80s PNG with junk bytes
            (d / "panel_right_80s.png").write_bytes(b"not a valid PNG")
            panels = load_panels(d)
            out = d / "right.bgra"
            painter = ClockPainter(GRAPHICS_FONTS, out)

            with self.assertLogs("wdbs.panels", level="WARNING") as logs:
                # Paint 80s with bad PNG -> None, should warn
                result_80s_1 = painter.paint(panels["80s"], dt.datetime(2026, 9, 28, 15, 7))
                self.assertIsNone(result_80s_1)

                # Paint 70s with good PNG -> should succeed
                result_70s = painter.paint(panels["70s"], dt.datetime(2026, 9, 28, 15, 7))
                self.assertEqual(result_70s, out)

                # Paint 80s again with bad PNG -> None, should NOT warn again (already in _warned set)
                result_80s_2 = painter.paint(panels["80s"], dt.datetime(2026, 9, 28, 15, 8))
                self.assertIsNone(result_80s_2)

            # Should have exactly one warning (for the first 80s failure)
            self.assertEqual(len(logs.output), 1)
            self.assertIn("80s", logs.output[0])


if __name__ == "__main__":
    unittest.main()
