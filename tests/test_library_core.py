"""WDBS Stream library (prodesk/stream/library_core.py): Stremio-compatible add/remove, Library tab rows."""

import copy
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

STREAM = Path(__file__).resolve().parents[1] / "prodesk/stream"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, STREAM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("stream_core")
lcore = _load("library_core")

NOW = datetime(2026, 9, 29, 4, 0, 0, 123000, tzinfo=timezone.utc)
TS = "2026-09-29T04:00:00.123Z"
META = {"id": "tt31170389", "type": "movie", "name": "Evil Dead Burn", "poster": "https://p/x.jpg"}


def stremio_item(**over):
    """Shape of a real api.strem.io library item (fields as the account returns them)."""
    it = {"_id": "tt31170389", "removed": True, "temp": True, "_ctime": "2026-07-16T22:45:27.793160272Z",
          "_mtime": "2026-07-16T22:45:46.805596739Z",
          "state": {"lastWatched": "2026-07-16T22:45:27.793163147Z", "timeWatched": 0, "timeOffset": 0,
                    "overallTimeWatched": 0, "timesWatched": 0, "flaggedWatched": 0, "duration": 0, "video_id": "",
                    "watched": "", "noNotif": False, "season": 0, "episode": 0},
          "name": "Evil Dead Burn", "type": "movie", "poster": "https://images.metahub.space/poster/small/tt31170389/img",
          "posterShape": "poster", "background": "", "logo": "", "year": ""}
    it.update(over)
    return it


class ToggleTests(unittest.TestCase):
    def test_add_new_item_is_full_and_not_temp(self):
        it = lcore.set_in_library(None, META, True, NOW)
        self.assertFalse(it["removed"] or it["temp"])
        self.assertEqual((it["_id"], it["type"], it["name"], it["poster"]), ("tt31170389", "movie", "Evil Dead Burn", "https://p/x.jpg"))
        self.assertEqual((it["_ctime"], it["_mtime"]), (TS, TS))
        self.assertEqual(it["state"]["timeOffset"], 0)
        self.assertIn("behaviorHints", it)
        self.assertTrue(lcore.in_library(it))

    def test_remove_missing_item_writes_nothing(self):
        self.assertIsNone(lcore.set_in_library(None, META, False, NOW))

    def test_add_existing_temp_item_keeps_everything_else(self):
        before = stremio_item(mystery={"a": 1})
        snap = copy.deepcopy(before)
        out = lcore.set_in_library(before, META, True, NOW)
        self.assertEqual(before, snap)                                   # input untouched
        self.assertEqual((out["removed"], out["temp"], out["_mtime"]), (False, False, TS))
        expect = {**snap, "removed": False, "temp": False, "_mtime": TS}
        self.assertEqual(out, expect)                                    # only those three fields change
        self.assertIsNot(out["state"], before["state"])

    def test_remove_keeps_state_and_temp(self):
        before = stremio_item(removed=False, temp=False, state={**stremio_item()["state"], "timeOffset": 5000, "x": 1})
        snap = copy.deepcopy(before)
        out = lcore.set_in_library(before, META, False, NOW)
        self.assertEqual(before, snap)
        self.assertEqual(out, {**snap, "removed": True, "_mtime": TS})
        self.assertFalse(lcore.in_library(out))

    def test_no_change_when_already_in_that_state(self):
        self.assertIsNone(lcore.set_in_library(stremio_item(removed=False, temp=False), META, True, NOW))
        self.assertIsNone(lcore.set_in_library(stremio_item(), META, False, NOW))

    def test_add_fills_missing_name_poster_only(self):
        out = lcore.set_in_library(stremio_item(name="", poster=None), META, True, NOW)
        self.assertEqual((out["name"], out["poster"]), ("Evil Dead Burn", "https://p/x.jpg"))
        out = lcore.set_in_library(stremio_item(), {**META, "name": "Other"}, True, NOW)
        self.assertEqual(out["name"], "Evil Dead Burn")

    def test_round_trip_restores_contents(self):
        before = stremio_item(removed=False, temp=False)
        back = lcore.set_in_library(lcore.set_in_library(before, META, False, NOW), META, True, NOW)
        self.assertEqual({k: v for k, v in back.items() if k != "_mtime"}, {k: v for k, v in before.items() if k != "_mtime"})


def item(id_, type_, ctime, last, removed=False, temp=False, **state):
    st = {"lastWatched": last, "timeOffset": 0, "duration": 0, "timesWatched": 0, "flaggedWatched": 0, "video_id": ""}
    st.update(state)
    return {"_id": id_, "name": id_, "type": type_, "removed": removed, "temp": temp, "_ctime": ctime, "_mtime": last,
            "state": st}


class RowsTests(unittest.TestCase):
    LIB = [item("m1", "movie", "2024-01-01T00:00:00Z", "2026-09-01T00:00:00Z", flaggedWatched=1),
           item("m2", "movie", "2026-09-20T00:00:00Z", "2026-09-20T00:00:00Z", timeOffset=600000, duration=6000000),
           item("s1", "series", "2023-01-01T00:00:00Z", "2026-09-10T00:00:00Z", video_id="s1:2:6", timeOffset=1,
                duration=2000000),
           item("t1", "movie", "2026-09-28T00:00:00Z", "2026-09-28T00:00:00Z", removed=True, temp=True),
           item("r1", "series", "2026-09-27T00:00:00Z", "2026-09-27T00:00:00Z", removed=True),
           item("c1", "channel", "2026-09-26T00:00:00Z", "2026-09-26T00:00:00Z")]

    def test_rows_and_order(self):
        snap = copy.deepcopy(self.LIB)
        rows = lcore.library_rows(self.LIB)
        self.assertEqual(self.LIB, snap)
        self.assertEqual([r["title"] for r in rows], ["Recently Added", "Series", "Movies"])
        self.assertEqual([c["id"] for c in rows[0]["items"]], ["m2", "m1", "s1"])     # by _ctime
        self.assertEqual([c["id"] for c in rows[2]["items"]], ["m2", "m1"])           # last watched first
        self.assertEqual([c["id"] for c in rows[1]["items"]], ["s1"])

    def test_badges(self):
        cards = {c["id"]: c for r in lcore.library_rows(self.LIB) for c in r["items"]}
        self.assertEqual(cards["m1"]["lib"], {"watched": True, "progress": None, "episode": None})
        self.assertEqual(cards["m2"]["lib"], {"watched": False, "progress": 0.1, "episode": None})
        self.assertEqual(cards["s1"]["lib"], {"watched": False, "progress": None, "episode": "S2 E6"})   # offset 1 = noise

    def test_empty(self):
        self.assertEqual(lcore.library_rows([]), [])
        self.assertEqual(lcore.library_rows([item("t", "movie", "x", "x", removed=True, temp=True)]), [])


if __name__ == "__main__":
    unittest.main()
