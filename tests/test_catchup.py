"""Unit tests for fs42_overlay/wdbs_catchup.py (pure logic; no FS42 needed)."""
import datetime as dt
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

spec = importlib.util.spec_from_file_location("wdbs_catchup", Path(__file__).parents[1] / "fs42_overlay" / "wdbs_catchup.py")
cu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cu)

F = lambda d: NS(content_type="feature", duration=d)
C = lambda d: NS(content_type="commercial", duration=d)
B = lambda d: NS(content_type="bump", duration=d)
W = lambda d: NS(content_type="weather", duration=d)


def pp(plan, index, offset):
    return NS(plan=plan, index=index, offset=offset)


class CatchupTests(unittest.TestCase):
    def test_feature_joined_late_restarts_segment(self):
        p = cu.adjust_play_point(pp([F(600), B(4)], 0, 47.5))
        self.assertEqual((p.index, p.offset), (0, 0))

    def test_feature_joined_deep_keeps_wall_clock(self):
        p = cu.adjust_play_point(pp([F(600)], 0, 377.0))  # tuning in mid-episode stays live
        self.assertEqual((p.index, p.offset), (0, 377.0))

    def test_near_end_commercial_is_skipped_and_chains(self):
        p = cu.adjust_play_point(pp([F(600), C(30), B(4), F(600)], 1, 22.2))
        self.assertEqual((p.index, p.offset), (2, 0))  # lands on the bump at 0
        p = cu.adjust_play_point(pp([C(60), F(600)], 0, 51.6))
        self.assertEqual((p.index, p.offset), (1, 0))

    def test_early_commercial_join_is_kept(self):
        p = cu.adjust_play_point(pp([C(60), F(600)], 0, 12.0))
        self.assertEqual((p.index, p.offset), (0, 12.0))

    def test_last_entry_never_skipped(self):
        p = cu.adjust_play_point(pp([F(600), C(30)], 1, 25.0))
        self.assertEqual((p.index, p.offset), (1, 25.0))

    def test_should_skip_break(self):
        self.assertTrue(cu.should_skip_break(C(30), 31))
        self.assertFalse(cu.should_skip_break(C(30), 29))
        self.assertFalse(cu.should_skip_break(F(30), 999))  # features are never skipped

    def test_weather_group_is_all_or_nothing(self):
        intro, ws = W(4), W(56)
        plan = [F(600), C(30), intro, ws, C(30), F(600)]
        cu._group_decisions.clear()
        # 06:09 case: 49 s late < 60 s group -> play the whole group, intro included
        self.assertFalse(cu.should_skip_break(intro, 49, plan))
        self.assertFalse(cu.should_skip_break(ws, 45, plan))
        cu._group_decisions.clear()
        # late by more than the group -> skip both; decision holds for the second entry
        self.assertTrue(cu.should_skip_break(intro, 61, plan))
        self.assertTrue(cu.should_skip_break(ws, 57, plan))
        self.assertFalse(cu.should_skip_break(C(30), 20, plan))  # commercial rule unchanged

    def test_weather_joined_mid_group_plays_rest(self):
        intro, ws = W(4), W(56)
        plan = [C(30), intro, ws, F(600)]
        cu._group_decisions.clear()
        self.assertFalse(cu.should_skip_break(ws, 999, plan))
        p = cu.adjust_play_point(pp(plan, 2, 50.0))  # near-end join into weather: keep it
        self.assertEqual((p.index, p.offset), (2, 50.0))

    def test_weather_uses_plan_from_annotate(self):
        intro, ws = W(4), W(56)
        p = pp([intro, ws, F(600)], 0, 0)
        liquid = NS(get_programming_block=lambda n, w: NS(start_time=dt.datetime(2026, 9, 27, 6), end_time=None))
        cu.annotate(liquid, "X", dt.datetime(2026, 9, 27, 6), p)
        cu._group_decisions.clear()
        self.assertTrue(cu.should_skip_break(intro, 100))  # no plan arg: uses the annotated block plan
        self.assertFalse(cu.should_skip_break(B(4), 3))     # bump unchanged

    def test_planned_start(self):
        start = dt.datetime(2026, 9, 26, 19, 0)
        p = pp([F(600), B(4), C(30)], 2, 0)
        p.wdbs_block_start = start
        self.assertEqual(cu.planned_start(p), start + dt.timedelta(seconds=604))

    def test_tail_hold(self):
        end = dt.datetime(2026, 9, 26, 19, 30)
        liquid = NS(get_programming_block=lambda n, w: NS(start_time=end - dt.timedelta(minutes=30), end_time=end))
        self.assertEqual(cu.tail_hold_until(liquid, "X", end - dt.timedelta(seconds=3)), end)
        self.assertIsNone(cu.tail_hold_until(liquid, "X", end - dt.timedelta(minutes=10)))  # not a rounding tail
        self.assertIsNone(cu.tail_hold_until(liquid, "X", end))


if __name__ == "__main__":
    unittest.main()
