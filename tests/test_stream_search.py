"""WDBS Stream search (prodesk/stream/search_core.py): search catalogs, query normalising, merging results."""

import importlib.util
import sys
import unittest
from pathlib import Path

STREAM = Path(__file__).resolve().parents[1] / "prodesk/stream"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, STREAM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if "stream_core" not in sys.modules:
    _load("stream_core")
sc = _load("search_core")

CINEMETA = {"transportUrl": "https://v3-cinemeta.strem.io/manifest.json", "manifest": {
    "id": "com.linvo.cinemeta", "name": "Cinemeta", "catalogs": [
        {"type": "movie", "id": "top", "extra": [{"name": "genre"}, {"name": "search"}, {"name": "skip"}]},
        {"type": "series", "id": "top", "extra": [{"name": "genre"}, {"name": "search"}, {"name": "skip"}]},
        {"type": "movie", "id": "year", "extra": [{"name": "genre", "isRequired": True}]},
        {"type": "series", "id": "last-videos", "extra": [{"name": "lastVideosIds", "isRequired": True}]}]}}
LEGACY = {"transportUrl": "https://legacy.example/manifest.json", "manifest": {
    "id": "legacy", "name": "Legacy", "catalogs": [
        {"type": "series", "id": "s", "extraSupported": ["search"], "extraRequired": ["search"]},
        {"type": "series", "id": "s2", "extraSupported": ["search"]},                # same addon + type: skipped
        {"type": "movie", "id": "g", "extraSupported": ["search", "genre"], "extraRequired": ["genre"]},
        {"type": "channel", "id": "c", "extra": [{"name": "search"}]},
        {"type": "movie", "id": "m", "extra": [{"name": "search", "isRequired": True}]}]}}
NOSEARCH = {"transportUrl": "https://torrentio.example/manifest.json", "manifest": {"id": "t", "catalogs": []}}


class CatalogTests(unittest.TestCase):
    def test_search_catalogs(self):
        cats = sc.search_catalogs([CINEMETA, NOSEARCH, LEGACY])
        self.assertEqual([(c["addon"], c["type"], c["id"]) for c in cats],
                         [("Cinemeta", "movie", "top"), ("Cinemeta", "series", "top"),
                          ("Legacy", "series", "s"), ("Legacy", "movie", "m")])
        self.assertEqual(len(sc.search_catalogs([CINEMETA, LEGACY], max_catalogs=3)), 3)
        self.assertEqual(sc.search_catalogs([NOSEARCH, {"manifest": {}}]), [])

    def test_search_url(self):
        cat = sc.search_catalogs([CINEMETA])[1]
        self.assertEqual(sc.search_url(cat, "breaking bad"),
                         "https://v3-cinemeta.strem.io/catalog/series/top/search=breaking%20bad.json")
        self.assertIn("search=a%2Fb%26c.json", sc.search_url(cat, "a/b&c"))

    def test_normalize_query(self):
        self.assertEqual(sc.normalize_query("  breaking   bad "), "breaking bad")
        self.assertIsNone(sc.normalize_query("a"))
        self.assertIsNone(sc.normalize_query(None))
        self.assertEqual(len(sc.normalize_query("x" * 200)), sc.MAX_QUERY)


def meta(id_, name, type_=None):
    m = {"id": id_, "name": name}
    if type_:
        m["type"] = type_
    return m


class MergeTests(unittest.TestCase):
    MOV = {"type": "movie"}
    SER = {"type": "series"}

    def test_dedupe_cap_and_card_shape(self):
        rows = sc.merge_results([(self.MOV, [meta("tt1", "Up"), meta("tt2", "Upgrade"), {"name": "no id"}]),
                                 (self.MOV, [meta("tt1", "Up (dup)"), meta("tt3", "Uptown")]),
                                 (self.SER, [meta("tt9", "Upload", "series")])], "zz", per_row=2)
        self.assertEqual([(r["title"], [i["id"] for i in r["items"]]) for r in rows],
                         [("Movies", ["tt1", "tt2"]), ("Series", ["tt9"])])
        it = rows[0]["items"][0]
        self.assertEqual((it["name"], it["type"]), ("Up", "movie"))       # first addon wins, type filled in
        self.assertIn("/background/medium/tt1/", it["background"])       # same card() as the browse rows

    def test_best_match_row_first(self):
        res = [(self.MOV, [meta("tt5", "The Bad Guys: Breaking In")]), (self.SER, [meta("tt0903747", "Breaking Bad")])]
        self.assertEqual([r["type"] for r in sc.merge_results(res, "breaking bad")], ["series", "movie"])
        self.assertEqual([r["type"] for r in sc.merge_results(res, "zzz")], ["movie", "series"])
        self.assertEqual([r["type"] for r in sc.merge_results(res, "breaking")], ["series", "movie"])

    def test_empty(self):
        self.assertEqual(sc.merge_results([(self.MOV, []), ({"type": "channel"}, [meta("x", "X")])], "xx"), [])


if __name__ == "__main__":
    unittest.main()
