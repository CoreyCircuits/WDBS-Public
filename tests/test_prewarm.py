"""weather-prewarm (fs42_overlay/wdbs_prewarm.py): when to warm, what to adopt, what to throw away."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

# The overlay module imports FS42's AutoBumpAgent lazily; give it a stand-in.
_agent = types.ModuleType("fs42.autobump_agent")
_agent.AutoBumpAgent = NS(is_autobump_url=lambda u: u.startswith(":autobump:="),
                          extract_url=lambda u: u[len(":autobump:="):])
_FAKE_FS42 = {"fs42": types.ModuleType("fs42"), "fs42.autobump_agent": _agent}

_spec = importlib.util.spec_from_file_location(
    "wdbs_prewarm", Path(__file__).resolve().parents[1] / "fs42_overlay" / "wdbs_prewarm.py")
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

WX = ":autobump:=http://ws4kp/index.html?a=1"


def e(path, dur, ct="commercial"):
    return NS(path=path, duration=dur, content_type=ct)


class Proc:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


class Base(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(sys.modules, _FAKE_FS42))  # only while each test runs


class ArmTickTests(Base):
    def setUp(self):
        super().setUp()
        self.ad, self.intro, self.wx, self.show = (e("/ad.mp4", 30), e("/intro.mp4", 4, "weather"),
                                                   e(WX, 56, "weather"), e("/show.mkv", 600, "feature"))
        self.pp = NS(plan=[self.ad, self.intro, self.wx, self.show])
        self.player = NS()

    def test_warms_lead_seconds_before_the_web_entry_across_the_intro(self):
        pw.arm(self.player, self.pp, self.ad)
        self.assertEqual(self.player._wdbs_prewarm_target, (self.wx, 4))
        with mock.patch.object(pw, "spawn") as spawn:
            pw.tick(self.player, pw.LEAD_S - 4 + 0.5)      # still too early
            spawn.assert_not_called()
            pw.tick(self.player, pw.LEAD_S - 4)
            spawn.assert_called_once_with(self.player, self.wx)
            pw.tick(self.player, 1)                        # once only
            spawn.assert_called_once()

    def test_never_looks_past_a_feature(self):
        pp = NS(plan=[self.ad, self.show, self.wx])
        pw.arm(self.player, pp, self.ad)
        self.assertIsNone(self.player._wdbs_prewarm_target)

    def test_disabled_by_env(self):
        with mock.patch.dict("os.environ", {"WDBS_PREWARM": "0"}):
            pw.arm(self.player, self.pp, self.intro)
        self.assertIsNone(self.player._wdbs_prewarm_target)

    def test_a_failed_spawn_leaves_the_stock_cold_start(self):
        pw.arm(self.player, self.pp, self.intro)
        with mock.patch.object(pw, "spawn", side_effect=OSError("no qt")), mock.patch.object(pw, "_close"):
            pw.tick(self.player, 1)
        self.assertIsNone(getattr(self.player, "_wdbs_warm", None))

    def test_url_matches_what_play_file_builds(self):
        self.assertEqual(pw.web_url(self.wx), "http://ws4kp/index.html?a=1&duration=56000")


class TakeDropTests(Base):
    def warm(self, player, path=WX, alive=True, entry=None):
        player._wdbs_warm = {"entry": entry, "path": path, "queue": "q", "proc": Proc(alive), "t": 0}

    def test_adopts_only_a_live_window_for_the_same_entry(self):
        p = NS()
        self.warm(p)
        with mock.patch.object(pw, "_close") as close:
            self.assertEqual(pw.take(p, WX)[0], "q")
            close.assert_not_called()
        self.assertIsNone(p._wdbs_warm)

    def test_other_entry_or_dead_window_is_closed_and_cold_starts(self):
        for path, alive in ((":autobump:=http://other", True), (WX, False)):
            p = NS()
            self.warm(p, alive=alive)
            with mock.patch.object(pw, "_close") as close:
                self.assertIsNone(pw.take(p, path))
                close.assert_called_once()

    def test_non_break_web_channels_never_adopt(self):
        p = NS()
        self.warm(p)
        with mock.patch.object(pw, "_close") as close:
            self.assertIsNone(pw.take(p, None))            # guide / Ch 4 show_web carry no wdbs_entry
            close.assert_called_once()

    def test_new_play_point_keeps_the_window_only_if_its_entry_is_in_the_plan(self):
        wx = e(WX, 56)
        p = NS()
        self.warm(p, entry=wx)
        with mock.patch.object(pw, "_close") as close:
            pw.drop_stale(p, NS(plan=[e("/x", 1), wx]))
            close.assert_not_called()
            pw.drop_stale(p, NS(plan=[e("/x", 1), e(WX, 56)]))   # equal path, different entry: a new plan
            close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
