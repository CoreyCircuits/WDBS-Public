import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from wdbs.bug import _UNKNOWN, HOLD_TICKS, RETRY_TICKS, Dresser, ManifestWatch, OVERLAY_ID, clear_overlays, decide, \
    era_on_screen, load_assets, overlay_steps, pick_asset, premultiplied_ok

ROOT = "/path/to/FieldStation42/catalog/example_sitcoms"
ERAS = {"sanford_and_son": "70s", "seinfeld": "90s", "atlanta": "00s"}


class EraOnScreenTests(unittest.TestCase):
    def test_episode_of_rotation_show_gets_its_era(self):
        self.assertEqual(era_on_screen(f"{ROOT}/sanford_and_son/Season 1/S01E01.avi", False, ERAS, ROOT), "70s")
        self.assertEqual(era_on_screen(f"{ROOT}/seinfeld/S01E01.mkv", False, ERAS, ROOT + "/"), "90s")

    def test_no_bug_outside_show_segments(self):
        for p in (f"{ROOT}/commercial/WDBS__x.mp4",
                  f"{ROOT}/bump/EXAMPLE_SITCOMS_ID.mp4",
                  f"{ROOT}/clips/latenight/x.mp4",
                  "/path/to/FieldStation42/wdbs_assets/era/id_70s.mp4",
                  "/path/to/FieldStation42/wdbs_assets/weather_intro.mp4",
                  ":autobump:=http://app-host.example.invalid:9090/index.html",
                  "/path/to/FieldStation42/runtime/standby.png",
                  "av://lavfi:color=c=black:s=1280x720:r=10",
                  f"{ROOT}/arrested_s1/ep.mkv"):             # legacy tag, not in the rotation
            self.assertIsNone(era_on_screen(p, False, ERAS, ROOT), p)

    def test_idle_empty_and_missing_inputs(self):
        self.assertIsNone(era_on_screen(None, False, ERAS, ROOT))
        self.assertIsNone(era_on_screen(f"{ROOT}/seinfeld/x.mkv", True, ERAS, ROOT))
        self.assertIsNone(era_on_screen(f"{ROOT}/seinfeld/x.mkv", False, ERAS, None))
        self.assertIsNone(era_on_screen(f"{ROOT}/seinfeld/x.mkv", False, {}, ROOT))


def _write_asset(d: Path, era: str, w=4, h=2, premultiplied=True, size_ok=True):
    (d / f"bug_{era}.json").write_text(json.dumps({"era": era, "w": w, "h": h, "x": 176, "y": 890}))
    px = bytes([10, 20, 30, 200]) if premultiplied else bytes([250, 20, 30, 100])   # B, G, R, A
    (d / f"bug_{era}.bgra").write_bytes(px * (w * h if size_ok else w * h - 1))


class BugAssetTests(unittest.TestCase):
    def test_loads_valid_pairs_and_skips_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_asset(d, "70s")
            _write_asset(d, "00s")
            _write_asset(d, "80s", size_ok=False)            # truncated copy
            _write_asset(d, "90s", premultiplied=False)      # straight alpha: mpv would blow it out
            assets = load_assets(d)
            self.assertEqual(sorted(assets), ["00s", "70s"])
            a = assets["70s"]
            self.assertEqual((a.x, a.y, a.w, a.h), (176, 890, 4, 2))
            (d / "bug_70s.json").write_text("{not json")
            self.assertEqual(sorted(load_assets(d)), ["00s"])

    def test_missing_era_falls_back_to_everyday_bug(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_asset(d, "00s")
            assets = load_assets(d)
            self.assertEqual(pick_asset(assets, "80s").era, "00s")
            self.assertEqual(pick_asset(assets, "00s").era, "00s")
            self.assertIsNone(pick_asset({}, "80s"))

    def test_premultiplied_check(self):
        self.assertTrue(premultiplied_ok(bytes([10, 20, 30, 200, 0, 0, 0, 0])))
        self.assertFalse(premultiplied_ok(bytes([250, 20, 30, 100])))
        self.assertFalse(premultiplied_ok(bytes([1, 2, 3])))


class ManifestWatchTests(unittest.TestCase):
    @staticmethod
    def _manifest(path, programs):
        path.write_text(json.dumps({"schema": 1, "channel": 10, "generated_at": "2026-09-28T00:00:00+00:00",
                                    "programs": programs}))

    def test_reloads_on_change_and_keeps_last_good_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.json"
            self._manifest(p, [{"tag": "seinfeld", "premiere_year": 1989}])
            w = ManifestWatch(p, check_every=60)
            self.assertEqual(w.get(0.0), {"seinfeld": "80s"})
            self._manifest(p, [{"tag": "seinfeld", "premiere_year": 1989, "era_override": "90s"}])
            os.utime(p, (time.time() + 5, time.time() + 5))
            self.assertEqual(w.get(30.0), {"seinfeld": "80s"})       # not re-checked yet
            self.assertEqual(w.get(61.0), {"seinfeld": "90s"})
            p.write_text("{broken")                                    # caught mid-write
            os.utime(p, (time.time() + 10, time.time() + 10))
            self.assertEqual(w.get(122.0), {"seinfeld": "90s"})       # keeps the last good map
            p.unlink()                                                 # the share dropped
            self.assertEqual(w.get(183.0), {"seinfeld": "90s"})

    def test_never_loaded_shows_nothing(self):
        w = ManifestWatch(Path(tempfile.gettempdir()) / "wdbs-no-such-manifest.json")
        self.assertEqual(w.get(0.0), {})


from wdbs.panels import LEFT_ID, RIGHT_ID, PanelAsset


def _panel(era):
    return PanelAsset(era, Path("l"), Path("r"), Path("p"), {"x": 16, "y": 452, "w": 208, "h": 176}, {})


class DressingTests(unittest.TestCase):
    PANELS = {"00s": _panel("00s"), "70s": _panel("70s")}

    def d(self, era, aspect, panels=None):
        from wdbs import bug
        bugs = {e: bug.BugAsset(e, Path(f"b{e}"), 1, 1, 0, 0) for e in ("00s", "70s", "90s")}
        return decide(era, aspect, bugs, self.PANELS if panels is None else panels, "15:07")

    def test_decide_nothing_off_show(self):
        self.assertEqual(self.d(None, 1.333), ("none",))   # commercials, IDs, web channels, guide, idle

    def test_decide_aspect_threshold(self):
        self.assertEqual(self.d("70s", 1.313), ("panels", "70s", "15:07"))   # The Nanny
        self.assertEqual(self.d("70s", 1.333), ("panels", "70s", "15:07"))
        self.assertEqual(self.d("70s", 1.334), ("panels", "70s", "15:07"))
        self.assertEqual(self.d("70s", 1.335), ("bug", "70s"))
        self.assertEqual(self.d("90s", 1.778), ("bug", "90s"))
        self.assertEqual(self.d("70s", None), ("bug", "70s"))

    def test_decide_panel_fallback_and_no_panels(self):
        self.assertEqual(self.d("80s", 1.333), ("panels", "00s", "15:07"))  # no 80s panels -> everyday set
        self.assertEqual(self.d("70s", 1.333, panels={}), ("bug", "70s"))   # none deployed -> today's behaviour

    def test_steps_from_unknown(self):
        self.assertEqual(overlay_steps(("none",), _UNKNOWN),
                         [("remove", LEFT_ID), ("remove", RIGHT_ID), ("remove", 41)])
        self.assertEqual(overlay_steps(("panels", "70s", "15:07"), _UNKNOWN),
                         [("remove", 41), ("add-left", "70s"), ("add-right", "70s", "15:07")])
        self.assertEqual(overlay_steps(("bug", "90s"), _UNKNOWN),
                         [("remove", LEFT_ID), ("remove", RIGHT_ID), ("add-bug", "90s")])

    def test_steps_between_states(self):
        p = ("panels", "70s", "15:07")
        self.assertEqual(overlay_steps(p, p), [])
        self.assertEqual(overlay_steps(("panels", "70s", "15:08"), p), [("add-right", "70s", "15:08")])
        self.assertEqual(overlay_steps(("panels", "80s", "15:07"), p),
                         [("add-left", "80s"), ("add-right", "80s", "15:07")])
        self.assertEqual(overlay_steps(("bug", "70s"), p),
                         [("remove", LEFT_ID), ("remove", RIGHT_ID), ("add-bug", "70s")])
        self.assertEqual(overlay_steps(p, ("bug", "70s")),
                         [("remove", 41), ("add-left", "70s"), ("add-right", "70s", "15:07")])
        self.assertEqual(overlay_steps(("none",), p), [("remove", LEFT_ID), ("remove", RIGHT_ID)])
        self.assertEqual(overlay_steps(("none",), ("bug", "70s")), [("remove", 41)])
        self.assertEqual(overlay_steps(("bug", "90s"), ("bug", "70s")), [("add-bug", "90s")])

    def test_add_right_falls_back_without_clock(self):
        from wdbs import bug
        panel = _panel("70s")

        class NoClock:
            def paint(self, panel, when):
                return None
        self.assertEqual(bug.right_panel_file(panel, NoClock()), panel.right_raw)


def _bugs():
    from wdbs import bug
    return {e: bug.BugAsset(e, Path(f"b{e}"), 1, 1, 0, 0) for e in ("00s", "70s", "90s")}


def _recorder():
    """A fake mpv command: records every call, returning "success" unless a queued failure comes up next.
    Push a string onto the returned `fail` list to make the next call (only) report that error."""
    calls, fail = [], []

    def cmd(*a):
        calls.append(a)
        return fail.pop(0) if fail else "success"
    return cmd, calls, fail


class ClearOverlaysTests(unittest.TestCase):
    def test_dry_run_sends_no_overlay_remove(self):
        cmd, calls, _fail = _recorder()
        clear_overlays(cmd, dry_run=True)
        self.assertEqual(calls, [])

    def test_normal_mode_removes_panels_and_bug(self):
        cmd, calls, _fail = _recorder()
        clear_overlays(cmd, dry_run=False)
        self.assertEqual([c[:2] for c in calls],
                         [("overlay-remove", LEFT_ID), ("overlay-remove", RIGHT_ID), ("overlay-remove", OVERLAY_ID)])


class DresserTests(unittest.TestCase):
    PANELS = {"00s": _panel("00s"), "70s": _panel("70s")}

    def test_partial_panel_failure_forgets_the_screen(self):
        cmd, calls, fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        d.update("70s", 1.335, "15:07")            # too-wide aspect: the bug goes up first
        self.assertEqual(d.shown, ("bug", "70s"))
        calls.clear()
        fail.extend(["success", "success", "no response"])   # switching to panels: the add-right (40) fails
        d.update("70s", 1.333, "15:07")
        self.assertIs(d.shown, _UNKNOWN)
        calls.clear()
        d.update(None, None, "15:07")               # next tick: commercial break
        self.assertIn(("overlay-remove", LEFT_ID), calls)
        self.assertIn(("overlay-remove", RIGHT_ID), calls)   # full cleanup, nothing left added

    def test_hold_lasts_while_the_file_is_still_loading(self):
        # 2026-09-28 16:00: The Nanny stalled 34 s under the start slate (FS42 retried the entry); the 20 s hold ran
        # out, the bug went up and showed for 0.3 s after the slate lifted. Loading ticks must not use up the hold.
        cmd, calls, _fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        d.update(None, None, "16:00")
        calls.clear()
        for _ in range(HOLD_TICKS * 3):
            d.update("90s", None, "16:00", loading=True)   # path set, nothing decoded yet (time-pos unavailable)
        self.assertEqual(calls, [])
        d.update("90s", 1.313, "16:00")
        adds = {c[1] for c in calls if c[0] == "overlay-add"}
        self.assertEqual(adds, {LEFT_ID, RIGHT_ID})

    def test_aspect_hold_avoids_a_bug_flash(self):
        cmd, calls, _fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        d.update(None, None, "15:07")                # commercial: settle at ("none",)
        calls.clear()
        for _ in range(26):                          # 13 s: a mid-episode tune-in seen live on 2026-09-28
            d.update("70s", None, "15:07")           # segment starts, aspect not reported yet
        self.assertEqual(calls, [])                  # held: not a single overlay command sent
        d.update("70s", 1.333, "15:07")               # aspect arrives inside the hold window
        adds = {c[1] for c in calls if c[0] == "overlay-add"}
        self.assertEqual(adds, {LEFT_ID, RIGHT_ID})   # panels went up, the bug never did

        cmd, calls, _fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        d.update(None, None, "15:07")
        calls.clear()
        for _ in range(HOLD_TICKS + 1):
            d.update("70s", None, "15:07")            # aspect never arrives, past HOLD_TICKS
        adds = {c[1] for c in calls if c[0] == "overlay-add"}
        self.assertEqual(adds, {OVERLAY_ID})          # gives up waiting and shows the bug

    def test_minute_change_repaints_only_the_right_panel(self):
        cmd, calls, _fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        d.update("70s", 1.333, "15:07")
        self.assertEqual(d.shown, ("panels", "70s", "15:07"))
        calls.clear()
        with self.assertNoLogs("wdbs.bug", level="INFO"):
            d.update("70s", 1.333, "15:08")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("overlay-add", RIGHT_ID))

    def test_reset_forces_full_redraw(self):
        cmd, calls, _fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        d.update("70s", 1.333, "15:07")
        calls.clear()
        d.reset()
        d.update("70s", 1.333, "15:07")               # same want, but shown was forgotten
        self.assertEqual([(c[0], c[1]) for c in calls],
                         [("overlay-remove", OVERLAY_ID), ("overlay-add", LEFT_ID), ("overlay-add", RIGHT_ID)])

    def test_dry_run_logs_once_per_change(self):
        cmd, calls, _fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS, dry_run=True)
        with self.assertLogs("wdbs.bug", level="INFO") as logs:
            d.update("70s", 1.333, "15:07")
            d.update("70s", 1.333, "15:08")           # clock-only change: no new "would" line
        self.assertEqual(calls, [])
        would_lines = [m for m in logs.output if "would show panels" in m]
        self.assertEqual(len(would_lines), 1)

    def test_default_painter_is_noop(self):
        cmd, calls, _fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS, painter=None)
        d.update("70s", 1.333, "15:07")
        add_right = next(c for c in calls if c[0] == "overlay-add" and c[1] == RIGHT_ID)
        self.assertEqual(add_right[4], str(self.PANELS["70s"].right_raw))

    def test_persistent_failure_backs_off_instead_of_redrawing_every_tick(self):
        def always_fail(*a):
            calls.append(a)
            return "no response"
        calls = []
        d = Dresser(always_fail, _bugs(), self.PANELS)
        for _ in range(40):
            d.update("70s", 1.333, "15:07")
        attempts = sum(1 for c in calls if c == ("overlay-remove", OVERLAY_ID))  # 1st step of a redraw from unknown
        self.assertGreaterEqual(attempts, 1)
        self.assertLessEqual(attempts, 3)

    def test_recovers_once_the_backoff_window_elapses(self):
        cmd, calls, fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        fail.extend(["success", "success", "no response"])   # first attempt: add-right fails
        d.update("70s", 1.333, "15:07")
        self.assertIs(d.shown, _UNKNOWN)
        calls.clear()
        for _ in range(RETRY_TICKS):                          # backing off: no redraw attempted
            d.update("70s", 1.333, "15:07")
        self.assertEqual(calls, [])
        d.update("70s", 1.333, "15:07")                        # backoff elapsed: retried, and now succeeds
        self.assertEqual(d.shown, ("panels", "70s", "15:07"))
        calls.clear()
        with self.assertNoLogs("wdbs.bug", level="INFO"):
            d.update("70s", 1.333, "15:08")                    # normal behaviour resumed
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("overlay-add", RIGHT_ID))

    def test_transition_to_none_is_attempted_during_backoff(self):
        cmd, calls, fail = _recorder()
        d = Dresser(cmd, _bugs(), self.PANELS)
        fail.extend(["success", "success", "no response"])
        d.update("70s", 1.333, "15:07")            # fails, enters backoff
        self.assertIs(d.shown, _UNKNOWN)
        calls.clear()
        d.update(None, None, "15:07")               # commercial break during backoff: cleared immediately
        self.assertEqual(sorted(c[1] for c in calls if c[0] == "overlay-remove"),
                         sorted([LEFT_ID, RIGHT_ID, OVERLAY_ID]))
        self.assertEqual(d.shown, ("none",))


if __name__ == "__main__":
    unittest.main()
