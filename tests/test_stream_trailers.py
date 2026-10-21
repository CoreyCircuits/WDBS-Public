"""WDBS Stream trailers (prodesk/stream/stream_core.py): normalizing Stremio trailer metadata, id validation, URL."""

import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("stream_core", Path(__file__).resolve().parents[1] / "prodesk/stream/stream_core.py")
core = importlib.util.module_from_spec(_spec)
sys.modules["stream_core"] = core
_spec.loader.exec_module(core)

# Shapes as Cinemeta returns them (movie/tt1375666, 2026-09-29)
INCEPTION = {"id": "tt1375666", "name": "Inception",
             "trailerStreams": [{"title": "Inception", "ytId": "cdx31ak4KbQ"}, {"title": "Inception", "ytId": "JE9z-gy4De4"}],
             "trailers": [{"source": "cdx31ak4KbQ", "type": "Trailer"}, {"source": "JE9z-gy4De4", "type": "Trailer"},
                          {"source": "Jvurpf91omw", "type": "Trailer"}]}


class TrailerTests(unittest.TestCase):
    def test_merges_both_shapes_dedup_in_order(self):
        self.assertEqual(core.trailers(INCEPTION), [
            {"title": "Inception", "ytId": "cdx31ak4KbQ"}, {"title": "Inception", "ytId": "JE9z-gy4De4"},
            {"title": "Inception", "ytId": "Jvurpf91omw"}])

    def test_legacy_only_uses_meta_name(self):
        m = {"name": "Breaking Bad", "trailers": [{"source": "VFLkMDEO-Xc", "type": "Trailer"},
                                                  {"source": "XZ8daibM3AE", "type": "Clip"}]}
        self.assertEqual(core.trailers(m), [{"title": "Breaking Bad", "ytId": "VFLkMDEO-Xc"}])

    def test_none_and_junk(self):
        self.assertEqual(core.trailers({}), [])
        self.assertEqual(core.trailers({"trailers": None, "trailerStreams": None}), [])
        junk = {"name": "X", "trailerStreams": [None, "cdx31ak4KbQ", {"ytId": "short"}, {"ytId": "cdx31ak4KbQ&x=1"},
                                                {"ytId": "https://youtu.be/cdx31ak4KbQ"}, {"ytId": 12345678901},
                                                {"ytId": "../../etc/pa"}, {"ytId": "--vo=null00"}],
                "trailers": [{"source": "a b c d e f"}, {"type": "Trailer"}]}
        # "--vo=null00" is 11 chars but '=' is outside the YouTube alphabet (ids only ever reach mpv inside a URL)
        self.assertEqual(core.trailers(junk), [])

    def test_cap(self):
        m = {"trailerStreams": [{"ytId": f"abcdefghij{c}"} for c in "0123456789"]}
        self.assertEqual(len(core.trailers(m)), core.MAX_TRAILERS)
        self.assertEqual(core.trailers(m)[0]["title"], "Trailer")

    def test_valid_id(self):
        for ok in ("cdx31ak4KbQ", "JE9z-gy4De4", "a_b-c_d-e_f"):
            self.assertTrue(core.valid_yt_id(ok), ok)
        for bad in ("", "cdx31ak4KbQx", "cdx31ak4Kb", "cdx31ak4Kb/", "cdx31ak4Kb\n", None, 5, ["cdx31ak4KbQ"]):
            self.assertFalse(core.valid_yt_id(bad), repr(bad))

    def test_url(self):
        self.assertEqual(core.trailer_url("cdx31ak4KbQ"), "https://www.youtube.com/watch?v=cdx31ak4KbQ")
        for bad in ("x", "cdx31ak4Kb&", None, "file:///etc/p"):
            with self.assertRaises(ValueError):
                core.trailer_url(bad)

    def test_format_caps_1080(self):
        self.assertIn("[height<=1080][width<=1920]", core.YTDL_FORMAT)
        self.assertNotIn("height>", core.YTDL_FORMAT)


if __name__ == "__main__":
    unittest.main()
