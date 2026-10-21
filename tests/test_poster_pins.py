"""prodesk/poster_pins.py: pin validation, rendering, and which cached Sonarr files need restoring."""

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "poster_pins", Path(__file__).resolve().parents[1] / "prodesk" / "poster_pins.py")
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def jpeg(w=680, h=1000, color=(200, 180, 20)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


class PinsTests(unittest.TestCase):
    def write(self, pins):
        d = Path(tempfile.mkdtemp())
        (d / "p.json").write_text(json.dumps({"pins": pins}))
        return d / "p.json"

    def test_repo_pins_are_valid(self):
        pins = pp.load_pins(pp.PINS)
        self.assertIn(1000001, {p["tvdb"] for p in pins})

    def test_rejects_non_tvdb_host(self):
        with self.assertRaises(ValueError):
            pp.load_pins(self.write([{"tvdb": 1, "title": "x", "poster": "http://example.com/a.jpg"}]))

    def test_rejects_missing_tvdb(self):
        with self.assertRaises(ValueError):
            pp.load_pins(self.write([{"title": "x", "poster": pp.ALLOWED_HOST + "a.jpg"}]))


class RenderTests(unittest.TestCase):
    def test_three_sizes_keep_aspect(self):
        from PIL import Image
        out = pp.render(jpeg())
        self.assertEqual(set(out), set(pp.SIZES))
        self.assertEqual(Image.open(io.BytesIO(out["poster.jpg"])).size, (680, 1000))
        self.assertEqual(Image.open(io.BytesIO(out["poster-500.jpg"])).size, (500, 735))
        self.assertEqual(Image.open(io.BytesIO(out["poster-250.jpg"])).size, (250, 368))


class StaleTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rendered = pp.render(jpeg())

    def test_missing_files_are_stale(self):
        self.assertEqual(sorted(pp.stale(self.dir, self.rendered)), sorted(pp.SIZES))

    def test_matching_files_are_left_alone(self):
        for name, data in self.rendered.items():
            (self.dir / name).write_bytes(data)
        self.assertEqual(pp.stale(self.dir, self.rendered), [])

    def test_sonarr_redownload_is_detected(self):
        for name, data in self.rendered.items():
            (self.dir / name).write_bytes(data)
        (self.dir / "poster.jpg").write_bytes(jpeg(color=(10, 10, 10)))  # Sonarr put TVDB's primary back
        self.assertEqual(pp.stale(self.dir, self.rendered), ["poster.jpg"])

    def test_write_atomic_leaves_no_temp(self):
        pp.write_atomic(self.dir / "poster.jpg", b"x")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["poster.jpg"])


class EnvTests(unittest.TestCase):
    def test_parses_quotes_export_and_comments(self):
        f = Path(tempfile.mkdtemp()) / "e"
        f.write_text("# c\nexport SONARR_URL='http://h:1'\nSONARR_API_KEY=\"k\"\n\n")
        self.assertEqual(pp.load_env(f), {"SONARR_URL": "http://h:1", "SONARR_API_KEY": "k"})


if __name__ == "__main__":
    unittest.main()
