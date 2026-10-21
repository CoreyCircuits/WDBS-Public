import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("stream_core", Path(__file__).resolve().parents[1] / "prodesk/stream/stream_core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


def ep(s, e, title=None):
    return {"id": f"tt1:{s}:{e}", "season": s, "episode": e, "title": title or f"Ep {e}"}


SEASONS = [{"season": 1, "episodes": [ep(1, 1), ep(1, 2)]}, {"season": 2, "episodes": [ep(2, 1, "Return")]},
           {"season": 0, "episodes": [ep(0, 1, "Special")]}]


class UpNextTests(unittest.TestCase):
    def test_next_and_previous_cross_seasons_and_skip_specials(self):
        self.assertEqual(core.adjacent_episode(SEASONS, "tt1:1:2", 1)["id"], "tt1:2:1")
        self.assertEqual(core.adjacent_episode(SEASONS, "tt1:2:1", -1)["id"], "tt1:1:2")
        self.assertIsNone(core.adjacent_episode(SEASONS, "tt1:2:1", 1))       # last regular episode
        self.assertIsNone(core.adjacent_episode(SEASONS, "tt1:1:1", -1))
        self.assertIsNone(core.adjacent_episode(SEASONS, "tt1:0:1", 1))       # specials have no neighbours
        self.assertIsNone(core.adjacent_episode(SEASONS, "nope", 1))

    def test_specials_only_show(self):
        only = [{"season": 0, "episodes": [ep(0, 1), ep(0, 2)]}]
        self.assertEqual(core.adjacent_episode(only, "tt1:0:1", 1)["id"], "tt1:0:2")

    def test_title_matches_what_the_ui_sends(self):
        self.assertEqual(core.episode_title("BoJack", ep(1, 3, "Prickly-Muffin")), "BoJack · S1 E3 · Prickly-Muffin")
        self.assertEqual(core.episode_title("Movie", None), "Movie")

    def test_pick_source_prefers_same_addon_and_skips_negative(self):
        ranked = [{"addon": "A", "score": 140, "url": "a"}, {"addon": "B", "score": 90, "url": "b"},
                  {"addon": "C", "score": -20, "url": "c"}]
        self.assertEqual(core.pick_source(ranked, "B")["url"], "b")
        self.assertEqual(core.pick_source(ranked, "Z")["url"], "a")
        self.assertEqual(core.pick_source(ranked, "C")["url"], "a")             # a bad source is never chosen
        self.assertIsNone(core.pick_source([{"addon": "C", "score": -1}], None))

    def test_related_movie_skips_current_and_seen(self):
        cands = [{"id": "tt9", "type": "movie"}, {"id": "tt8", "type": "movie"}, {"id": "tt7", "type": "series"},
                 {"id": "tt6", "type": "movie"}]
        self.assertEqual(core.related_movie(cands, "tt9", {"tt8"})["id"], "tt6")
        self.assertIsNone(core.related_movie(cands[:2], "tt9", {"tt8"}))

    def test_seen_ids(self):
        lib = [{"_id": "a", "state": {"timesWatched": 1}}, {"_id": "b", "state": {"timeOffset": 5000}},
               {"_id": "c", "state": {}}, {"_id": "d", "state": {"flaggedWatched": 1}}]
        self.assertEqual(core.seen_ids(lib), {"a", "b", "d"})


if __name__ == "__main__":
    unittest.main()
