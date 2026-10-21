"""WDBS Stream core (prodesk/stream/stream_core.py): rows, Continue Watching, resume, library writes, stream ranking."""

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location("stream_core", Path(__file__).resolve().parents[1] / "prodesk/stream/stream_core.py")
core = importlib.util.module_from_spec(_spec)
sys.modules["stream_core"] = core
_spec.loader.exec_module(core)

NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
CINEMETA = {"transportUrl": "https://v3-cinemeta.strem.io/manifest.json", "manifest": {
    "id": "com.linvo.cinemeta", "name": "Cinemeta", "types": ["movie", "series"], "idPrefixes": ["tt"],
    "resources": ["catalog", "meta"],
    "catalogs": [{"type": "movie", "id": "top", "name": "Popular"}, {"type": "series", "id": "top", "name": "Popular"},
                 {"type": "movie", "id": "search", "name": "Search", "extra": [{"name": "search", "isRequired": True}]}]}}
TORRENTIO = {"transportUrl": "https://torrentio.example/realdebrid=KEY/manifest.json", "manifest": {
    "id": "torrentio", "name": "Torrentio", "types": ["movie", "series"], "idPrefixes": ["tt"],
    "resources": [{"name": "stream", "types": ["movie", "series"], "idPrefixes": ["tt"]}], "catalogs": []}}


class AddonTests(unittest.TestCase):
    def test_rows_skip_required_extras(self):
        rows = core.catalog_rows([CINEMETA, TORRENTIO])
        self.assertEqual([(r["type"], r["id"]) for r in rows], [("movie", "top"), ("series", "top")])
        self.assertEqual([r["type"] for r in core.catalog_rows([CINEMETA], "series")], ["series"])

    def test_resource_urls_and_support(self):
        self.assertEqual(core.resource_url(TORRENTIO["transportUrl"], "stream", "series", "tt1:2:3"),
                         "https://torrentio.example/realdebrid=KEY/stream/series/tt1%3A2%3A3.json")
        self.assertTrue(core.supports(TORRENTIO["manifest"], "stream", "movie", "tt123"))
        self.assertFalse(core.supports(TORRENTIO["manifest"], "stream", "movie", "kitsu:1"))
        self.assertFalse(core.supports(CINEMETA["manifest"], "stream", "movie", "tt123"))

    def test_art_from_metahub_for_imdb_ids(self):
        a = core.art({"id": "tt0903747"})
        self.assertIn("/background/medium/tt0903747/", a["background"])
        self.assertIsNone(core.art({"id": "kitsu:1"})["background"])


def lib_item(id_, type_, off, dur, vid, last, removed=False, temp=False):
    return {"_id": id_, "name": id_, "type": type_, "removed": removed, "temp": temp, "_mtime": last,
            "state": {"timeOffset": off, "duration": dur, "video_id": vid, "lastWatched": last}}


class LibraryTests(unittest.TestCase):
    def test_continue_watching_order_and_filter(self):
        lib = [lib_item("tt1", "movie", 60000, 600000, "tt1", "2026-09-01T00:00:00.000Z"),
               lib_item("tt2", "series", 1000, 2000000, "tt2:3:4", "2026-09-02T00:00:00.000Z", temp=True, removed=True),
               lib_item("tt3", "movie", 0, 0, None, "2026-09-03T00:00:00.000Z"),
               lib_item("tt4", "movie", 5, 10, "tt4", "2026-09-04T00:00:00.000Z", removed=True)]
        cw = core.continue_watching(lib)
        self.assertEqual([c["id"] for c in cw], ["tt2", "tt1"])
        self.assertEqual(cw[0]["episode"], "S3 E4")
        self.assertEqual(cw[1]["progress"], 0.1)

    def test_resume(self):
        it = lib_item("tt1", "series", 871000, 2400000, "tt1:1:2", "x")
        self.assertEqual(core.resume_offset(it, "tt1:1:2"), 868.0)
        self.assertEqual(core.resume_offset(it, "tt1:1:3"), 0.0)           # other episode starts at 0
        it["state"]["timeOffset"] = 2300000
        self.assertEqual(core.resume_offset(it, "tt1:1:2"), 0.0)           # finished: start over

    def test_progress_update_keeps_unknown_fields(self):
        it = lib_item("tt1", "movie", 0, 0, None, "x")
        it["mystery"] = 1
        out = core.progress_update(it, "tt1", 871.5, 6000.0, 300.0, NOW)
        st = out["state"]
        self.assertEqual((st["timeOffset"], st["duration"], st["video_id"]), (871500, 6000000, "tt1"))
        self.assertEqual(st["timeWatched"], 300000)
        self.assertEqual(out["mystery"], 1)
        self.assertEqual(out["_mtime"], "2026-09-29T04:00:00.000Z")
        self.assertEqual(it["state"]["timeOffset"], 0)                     # input untouched
        done = core.progress_update(it, "tt1", 5700.0, 6000.0, 60.0, NOW)["state"]
        self.assertEqual((done["timeOffset"], done["flaggedWatched"], done["timesWatched"]), (0, 1, 1))

    def test_new_item_is_temp(self):
        it = core.new_library_item({"id": "tt9", "name": "X", "type": "movie"}, NOW)
        self.assertTrue(it["temp"] and it["removed"])
        self.assertEqual(it["state"]["lastWatched"], "2026-09-29T04:00:00.000Z")


class StreamTests(unittest.TestCase):
    def test_ranking(self):
        s = [("Torrentio", {"name": "Torrentio\n4k", "title": "Show.2160p.x265\n👤 10 💾 30 GB", "url": "https://rd/a"}),
             ("Torrentio", {"name": "[RD+] Torrentio\n1080p", "title": "Show.1080p.WEB\n💾 2.1 GB", "url": "https://rd/b"}),
             ("Torrentio", {"name": "Torrentio\n1080p", "title": "Show.1080p", "infoHash": "a" * 40, "fileIdx": 1}),
             ("Torrentio", {"name": "Torrentio", "title": "Show HDCAM", "url": "https://rd/c"}),
             ("Torrentio", {"name": "no way to play", "infoHash": "b" * 40}),
             ("YT", {"name": "trailer", "ytId": "abc"})]
        r = core.rank_streams(s)
        self.assertEqual([x["url"] for x in r][:2], ["https://rd/b", "https://rd/a"])
        self.assertEqual(r[0]["quality"], "1080p")
        self.assertTrue(r[0]["cached"])
        self.assertEqual(r[0]["size"], "2.1 GB")
        self.assertIn("http://127.0.0.1:11470/" + "a" * 40 + "/1", [x["url"] for x in r])
        self.assertEqual(len(r), 4)                                        # unplayable ones dropped
        self.assertEqual(r[-1]["url"], "https://rd/c")                     # CAM last


if __name__ == "__main__":
    unittest.main()


class LanguageTests(unittest.TestCase):
    """Owner 9/29: a Spanish release got picked. English/unmarked first, foreign-only far down, never dropped."""

    def test_language_labels(self):
        self.assertEqual(core.language("Show.1080p.WEB 🇬🇧"), (0, None))
        self.assertEqual(core.language("Show.1080p.BluRay.x264"), (0, None))
        self.assertEqual(core.language("Show 1080p LATINO 🇲🇽")[1], "Spanish")
        self.assertEqual(core.language("Show VOSTFR 1080p")[1], "French")
        self.assertEqual(core.language("Show 1080p 🇪🇸 / 🇲🇽")[1], "Foreign language")
        self.assertEqual(core.language("Show.1080p.MULTI 🇬🇧 / 🇪🇸"), (core.MULTI_PENALTY, "Multi-language"))

    def test_english_outranks_foreign(self):
        s = [("Torrentio", {"name": "[RD+] Torrentio\n1080p", "title": "Show.1080p.CASTELLANO 🇪🇸\n💾 2 GB", "url": "https://rd/es"}),
             ("Torrentio", {"name": "[RD+] Torrentio\n720p", "title": "Show.720p.WEB\n💾 1 GB", "url": "https://rd/en"})]
        r = core.rank_streams(s)
        self.assertEqual([x["url"] for x in r], ["https://rd/en", "https://rd/es"])
        self.assertEqual(r[1]["language"], "Spanish")

    def test_overlay_sources(self):
        ranked = [{"quality": "1080p", "cached": True, "size": "2.1 GB", "addon": "Torrentio", "url": "a"},
                  {"quality": None, "cached": False, "size": None, "addon": "Comet", "language": "Spanish", "url": "b"}]
        out = core.overlay_sources(ranked, "b")
        self.assertEqual(out[0], {"label": "1080p ⚡ 2.1 GB", "detail": "Torrentio", "current": False})
        self.assertEqual(out[1], {"label": "Source", "detail": "Spanish · Comet", "current": True})
