"""Remote v2 pure logic (fs42_overlay/wdbs_remote_core.py)."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "wdbs_remote_core", Path(__file__).resolve().parents[1] / "fs42_overlay" / "wdbs_remote_core.py")
core = importlib.util.module_from_spec(_spec)
sys.modules["wdbs_remote_core"] = core
_spec.loader.exec_module(core)


class CommandTests(unittest.TestCase):
    def test_valid_commands(self):
        self.assertEqual(core.parse_command("tune:10"), ("tune", "10"))
        self.assertEqual(core.parse_command("key:PageUp"), ("key", "PageUp"))
        self.assertEqual(core.parse_command("key:ArrowLeft"), ("key", "ArrowLeft"))
        self.assertEqual(core.parse_command("guide"), ("guide", None))
        for k in ("Back", "PlayPause", "FastForward", "Rewind"):   # remote-app keys (Channel Router)
            self.assertEqual(core.parse_command(f"key:{k}"), ("key", k))

    def test_rejected_commands(self):
        for bad in ("stop", "exit", "tune:", "tune:abc", "tune:0", "tune:1000", "key:Escape", "key:arrowup", "guide:5", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.parse_command(bad)


class GuardTests(unittest.TestCase):
    def test_repeat_within_window_is_dropped(self):
        g = core.Guard()
        self.assertIsNone(g.check(0.0, "mute"))           # (VOL +/- and D-pad keys are rapid since 9/29)
        g.ran(0.0, "mute")
        self.assertEqual(g.check(0.4, "mute"), "repeat")
        self.assertIsNone(g.check(0.7, "mute"))
        self.assertIsNone(g.check(0.4, "subs"))           # a different command is not a repeat

    def test_tune_is_busy_until_the_target_lands(self):
        g = core.Guard()
        g.observe(0.0, 10)
        g.ran(0.0, "tune:4", target=4)
        self.assertEqual(g.check(1.0, "ch_up"), "busy")
        self.assertIsNone(g.check(1.0, "vol_up"))          # non-tune commands pass while busy
        g.observe(2.0, 4)
        self.assertIsNone(g.check(2.1, "ch_up"))

    def test_up_down_lands_on_any_other_channel(self):
        g = core.Guard()
        g.observe(0.0, 10)
        g.ran(0.0, "ch_up")
        g.observe(1.0, 10)
        self.assertEqual(g.check(1.1, "ch_down"), "busy")
        g.observe(1.5, 90)
        self.assertIsNone(g.check(1.6, "ch_down"))

    def test_busy_expires_without_status_reads(self):
        g = core.Guard()
        g.observe(0.0, 10)
        g.ran(0.0, "tune:4", target=4)
        self.assertIsNone(g.check(core.SETTLE_S + 0.1, "tune:3"))

    def test_previous_channel_follows_any_change(self):
        g = core.Guard()
        g.observe(0.0, 10)
        self.assertIsNone(g.previous)
        g.observe(1.0, 99)                                  # changed elsewhere (stock remote, guide button)
        self.assertEqual((g.current, g.previous), (99, 10))
        g.observe(2.0, 10)                                  # BACK toggles between the two
        self.assertEqual((g.current, g.previous), (10, 99))
        g.observe(3.0, None)                                # an unreadable status changes nothing
        self.assertEqual((g.current, g.previous), (10, 99))


CAT = "/path/to/FieldStation42/catalog/example_sitcoms"
NANNY = f"{CAT}/nanny/Season 1/The Nanny - S01E09 - Personal Business.mkv"
BMW = f"{CAT}/boy_meets_world/Season 1/Boy.Meets.World.S01E12.Once.in.Love.with.Amy.DVDRip.XviD-MEDiEVAL.avi"
AD = f"{CAT}/commercial/WDBS__2005-09__2007---Zyrtec-Drug-Commercial__45s__617924253fdb.mp4"
WX = ":autobump:=http://app-host.example.invalid:9090/index.html?x=1"


def st(path, ct, ch=10, title="The Nanny", dur="0:11:37/0:22:05", ts="2026-09-28T13:36:10"):
    return {"channel_number": ch, "network_name": "EXAMPLE SITCOMS", "title": title, "duration": dur,
            "timestamp": ts, "file_path": path, "content_type": ct}


class ClassifyTests(unittest.TestCase):
    def test_episode_sonarr_and_scene_names(self):
        c = core.classify(st(NANNY, "feature"), None)
        self.assertEqual((c["kind"], c["show_tag"], c["episode"], c["episode_title"]),
                         ("episode", "nanny", "S01E09", "Personal Business"))
        c = core.classify(st(BMW, "feature"), None)
        self.assertEqual((c["show_tag"], c["episode"], c["episode_title"]),
                         ("boy_meets_world", "S01E12", "Once in Love with Amy"))

    def test_commercial_weather_bump(self):
        c = core.classify(st(AD, "commercial"), None)
        self.assertEqual((c["kind"], c["commercial_file"]), ("commercial", os.path.basename(AD)))
        self.assertEqual(core.classify(st(WX, "weather"), None)["kind"], "weather")
        intro = "/path/to/FieldStation42/wdbs_assets/weather_intro.mp4"
        self.assertEqual(core.classify(st(intro, "weather"), None)["kind"], "weather")
        self.assertEqual(core.classify(st(f"{CAT}/bump/EXAMPLE_SITCOMS_ID.mp4", "bump"), None)["kind"], "bump")

    def test_web_channels(self):
        for ch, kind in ((99, "guide"), (3, "today"), (4, "weather"), (42, "web")):
            s = {"channel_number": ch, "network_name": "X", "title": "X", "content_type": "web"}
            self.assertEqual(core.classify(s, None)["kind"], kind)

    def test_standby_wins_but_keeps_details(self):
        c = core.classify(st(NANNY, "feature"), "stall")
        self.assertEqual((c["kind"], c["show_tag"]), ("standby", "nanny"))

    def test_empty_status_rates_as_unknown(self):
        self.assertEqual(core.classify({}, None)["kind"], "unknown")


class PositionTests(unittest.TestCase):
    def test_live_position_adds_elapsed_and_caps(self):
        s = st(NANNY, "feature", dur="0:11:37/0:05:48", ts="2026-09-28T13:36:10")
        pos, seg = core.live_position(s, datetime(2026, 9, 28, 13, 37, 10))
        self.assertEqual((pos, seg), (697 + 60, 348))
        pos, _ = core.live_position(s, datetime(2026, 9, 28, 14, 0, 0))
        self.assertEqual(pos, 697 + 348)
        self.assertEqual(core.fmt_hms(757), "0:12:37")
        self.assertEqual(core.live_position({}, datetime(2026, 9, 28)), (None, None))

    def test_scheduled_entry_and_lateness(self):
        start = datetime(2026, 9, 28, 13, 30)
        plan = [(NANNY, 600.0, "feature", 0.0), (AD, 45.0, "commercial", 0.0), (NANNY, 700.0, "feature", 600.0)]
        now = datetime(2026, 9, 28, 13, 40, 20)
        sched = core.scheduled_entry(start, plan, now)
        self.assertEqual((sched["path"], sched["planned_start"]), (AD, "2026-09-28T13:40:00"))
        self.assertEqual(core.late_seconds(sched, AD, 15.0, now), 5.0)     # 20 s since planned, 15 s in
        self.assertIsNone(core.late_seconds(sched, NANNY, 15.0, now))       # something else is playing
        self.assertIsNone(core.scheduled_entry(start, plan, datetime(2026, 9, 28, 14, 30)))


class RecordTests(unittest.TestCase):
    def rec(self, **kw):
        args = dict(rid="r1", thumb="down", reason=None, status=st(AD, "commercial"),
                    now_local=datetime(2026, 9, 28, 13, 36, 40), slate=None, sched=None, log_lines=["WARNING x"])
        args.update(kw)
        return core.rating_record(**args)

    def test_record_fields(self):
        r = self.rec()
        self.assertEqual((r["id"], r["thumb"], r["kind"], r["channel"], r["daypart"]),
                         ("r1", "down", "commercial", 10, "Daytime"))
        self.assertEqual(r["label"], "Zyrtec Drug Commercial")
        self.assertEqual(r["position"], "0:12:07")
        self.assertEqual(r["log"], ["WARNING x"])

    def test_bad_thumb_or_reason_rejected(self):
        with self.assertRaises(ValueError):
            self.rec(thumb="meh")
        with self.assertRaises(ValueError):
            self.rec(thumb="up", reason="Audio")                # reasons are for 👎 only
        with self.assertRaises(ValueError):
            core.with_reason(self.rec(), "Too spicy")

    def test_with_reason_keeps_the_original_snapshot(self):
        r = self.rec()
        r2 = core.with_reason(r, "Glitch/stall")
        self.assertEqual((r2["reason"], r2["file"], r2["at"]), ("Glitch/stall", AD, r["at"]))

    def test_labels(self):
        self.assertEqual(self.rec(status=st(NANNY, "feature"))["label"], "The Nanny S01E09")
        s = {"channel_number": 99, "network_name": "TV_HOST GUIDE", "title": "TV_HOST GUIDE", "content_type": "web"}
        self.assertEqual(self.rec(status=s)["label"], "the guide")
        self.assertEqual(self.rec(status=st(WX, "weather"))["label"], "WeatherCast weather")
        self.assertEqual(self.rec(status=st(NANNY, "feature"), slate="stall")["label"], "standby slate over The Nanny")

    def test_append_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a" / "ratings.jsonl"
            core.append_jsonl(p, {"id": "1", "label": "Café"})
            core.append_jsonl(p, {"id": "2"})
            self.assertEqual([json.loads(l)["id"] for l in p.read_text(encoding="utf-8").splitlines()], ["1", "2"])

    def test_dayparts_and_logs(self):
        d = datetime(2026, 9, 28)
        self.assertEqual([core.daypart(d.replace(hour=h)) for h in (0, 1, 5, 8, 12, 16, 19, 22)],
                         ["Late Night", "Overnight", "Early", "Morning", "Daytime", "After School", "Prime", "Late Night"])
        lines = ["INFO ok", "WARNING slow", "ERROR boom", "Start timed out", "INFO fine"] + ["ERROR e"] * 30
        picked = core.pick_log_lines(lines)
        self.assertEqual(len(picked), 20)
        self.assertNotIn("INFO ok", core.pick_log_lines(lines[:5]))

    def test_info_text(self):
        t = core.info_text(st(NANNY, "feature"), "S01E09", "Personal Business",
                           (datetime(2026, 9, 28, 16, 0), "Sanford and Son"))
        self.assertEqual(t, "Ch 10 · The Nanny · S01E09 Personal Business · Next 4:00 Sanford and Son")


EDT = timezone(timedelta(hours=-4))


def ev(title, h1, m1, h2, m2, all_day=False):
    return {"title": title, "start": datetime(2026, 9, 28, h1, m1, tzinfo=EDT).isoformat(),
            "end": datetime(2026, 9, 28, h2, m2, tzinfo=EDT).isoformat(), "all_day": all_day}


FEED = {"events": [ev("Morning routine", 8, 30, 9, 30), ev("Day off", 0, 0, 23, 59, all_day=True),
                   ev("Priority work: errands, bills", 10, 0, 12, 0), ev("Lunch", 12, 30, 13, 0)],
        "weather": {"temp": 75.4, "sky": "Clear", "high": 79, "low": 50}}


def at(h, m):
    return datetime(2026, 9, 28, h, m, tzinfo=EDT)


class StripTests(unittest.TestCase):
    def test_in_a_block(self):
        s = core.day_strip(FEED, at(11, 22))
        self.assertEqual(s, {"line1": "Priority work: errands, bills · 38 min left",
                             "line2": "Next 12:30 Lunch · 75° Clear"})

    def test_long_block_shows_hours(self):
        self.assertEqual(core.day_strip(FEED, at(10, 0))["line1"],
                         "Priority work: errands, bills · 2 h left")

    def test_between_blocks_and_boundary(self):
        self.assertEqual(core.day_strip(FEED, at(12, 0))["line1"], "Free until 12:30")   # ended exactly now

    def test_after_the_last_block(self):
        self.assertEqual(core.day_strip(FEED, at(20, 0)),
                         {"line1": "Nothing else on the calendar today", "line2": "75° Clear"})

    def test_no_weather_and_no_feed(self):
        self.assertEqual(core.day_strip({"events": FEED["events"]}, at(11, 22))["line2"], "Next 12:30 Lunch")
        self.assertIsNone(core.day_strip(None, at(11, 22)))
        self.assertEqual(core.day_strip({"events": [{"title": "bad"}]}, at(11, 22))["line1"],
                         "Nothing else on the calendar today")

    def test_tv_line(self):
        self.assertEqual(core.tv_line({"channel_number": 10, "title": "The Nanny"}), "📺 10 · The Nanny")

    def test_tv_line_without_status(self):
        self.assertEqual(core.tv_line({}), "📺 TV not responding")


class SpotNameTests(unittest.TestCase):
    def test_triple_dash_inside_a_spot_name_reads_as_a_separator(self):
        rec = {"kind": "commercial", "commercial_file": "WDBS__2005-09__2007---CBS-Survivor-Promo---News-6-Promo__30s__ab12.mp4"}
        self.assertEqual(core.rating_label(rec), "CBS Survivor Promo · News 6 Promo")

    def test_plain_spot_name_unchanged(self):
        rec = {"kind": "commercial", "commercial_file": os.path.basename(AD)}
        self.assertEqual(core.rating_label(rec), "Zyrtec Drug Commercial")


def sub(tid, lang=None, title=None):
    return {"type": "sub", "id": tid, "lang": lang, "title": title}


VIDEO = {"type": "video", "id": 1}
AUDIO = {"type": "audio", "id": 1, "lang": "eng"}


class SubtitleCycleTests(unittest.TestCase):
    """SUBS walks every subtitle track, then OFF, then back to the first track."""

    def test_no_subtitle_tracks(self):
        self.assertEqual(core.next_subtitle([VIDEO, AUDIO], True, False), (None, "No subtitles on this"))

    def test_off_turns_on_the_first_track(self):
        tracks = [VIDEO, AUDIO, sub(1, "eng"), sub(2, "spa")]
        self.assertEqual(core.next_subtitle(tracks, False, 1), ({"sid": 1, "sub-visibility": True}, "SUBS: English"))

    def test_nothing_selected_turns_on_the_first_track(self):
        tracks = [sub(3, "eng"), sub(4, "spa")]
        self.assertEqual(core.next_subtitle(tracks, True, False), ({"sid": 3, "sub-visibility": True}, "SUBS: English"))

    def test_next_english_track(self):
        tracks = [sub(1, "eng"), sub(2, "spa"), sub(3, "eng", "SDH")]
        self.assertEqual(core.next_subtitle(tracks, True, 1), ({"sid": 3, "sub-visibility": True}, "SUBS: English (SDH)"))

    def test_last_english_track_goes_off(self):
        tracks = [sub(1, "eng"), sub(2, "spa"), sub(3, "eng", "SDH")]
        self.assertEqual(core.next_subtitle(tracks, True, 3), ({"sub-visibility": False}, "SUBS: OFF"))

    def test_other_languages_are_skipped(self):
        # a real Nanny file carries 29 tracks; SUBS must not walk all of them to reach OFF
        tracks = [sub(1, "eng")] + [sub(i, "ara") for i in range(2, 30)]
        self.assertEqual(core.next_subtitle(tracks, True, 1), ({"sub-visibility": False}, "SUBS: OFF"))

    def test_a_foreign_track_that_is_on_goes_off(self):
        tracks = [sub(1, "eng"), sub(2, "spa")]
        self.assertEqual(core.next_subtitle(tracks, True, 2)[1], "SUBS: English")

    def test_no_english_uses_the_first_track_then_off(self):
        tracks = [sub(4, "fre"), sub(5, "ger")]
        self.assertEqual(core.next_subtitle(tracks, False, False), ({"sid": 4, "sub-visibility": True}, "SUBS: FRE"))
        self.assertEqual(core.next_subtitle(tracks, True, 4), ({"sub-visibility": False}, "SUBS: OFF"))

    def test_single_track_toggles_on_and_off(self):
        tracks = [sub(1, "eng")]
        self.assertEqual(core.next_subtitle(tracks, True, 1), ({"sub-visibility": False}, "SUBS: OFF"))
        self.assertEqual(core.next_subtitle(tracks, False, 1)[1], "SUBS: English")

    def test_track_names(self):
        self.assertEqual(core.next_subtitle([sub(1, None, "SDH")], False, False)[1], "SUBS: SDH")
        self.assertEqual(core.next_subtitle([sub(1, "eng", "English")], False, False)[1], "SUBS: English")
        self.assertEqual(core.next_subtitle([sub(1, "fre")], False, False)[1], "SUBS: FRE")
        self.assertEqual(core.next_subtitle([sub(5)], False, False)[1], "SUBS: Track 5")


class ContentKeyTests(unittest.TestCase):
    """The 👍/👎 halo: what's on screen now matched against earlier ratings."""

    def test_keys_from_status(self):
        self.assertEqual(core.content_key(st(NANNY, "feature"), None), "file:" + NANNY)
        self.assertEqual(core.content_key(st(AD, "commercial"), None), "spot:" + os.path.basename(AD))
        guide = {"channel_number": 99, "network_name": "TV_HOST GUIDE", "title": "x", "content_type": "web"}
        self.assertEqual(core.content_key(guide, None), "guide:99")
        self.assertEqual(core.content_key(st(WX, "weather"), None), "weather:10")
        self.assertIsNone(core.content_key({}, None))

    def test_a_record_has_the_same_key_as_its_status(self):
        for s in (st(NANNY, "feature"), st(AD, "commercial"), st(WX, "weather")):
            rec = core.rating_record(rid="r", thumb="up", reason=None, status=s, now_local=datetime(2026, 9, 28, 16, 0),
                                     slate=None, sched=None, log_lines=[])
            self.assertEqual(core.record_key(rec), core.content_key(s, None))

    def test_standby_slate_rates_the_item_underneath(self):
        self.assertEqual(core.content_key(st(NANNY, "feature"), "stall"), "file:" + NANNY)
        for s in (st(NANNY, "feature"), st(AD, "commercial")):
            rec = core.rating_record(rid="r", thumb="down", reason=None, status=s, now_local=datetime(2026, 9, 28, 16, 0),
                                     slate="stall", sched=None, log_lines=[])
            self.assertEqual(rec["kind"], "standby")
            self.assertEqual(core.record_key(rec), core.content_key(s, "stall"))

    def test_latest_rating_wins(self):
        recs = [{"kind": "commercial", "commercial_file": "a.mp4", "thumb": "up"},
                {"kind": "commercial", "commercial_file": "a.mp4", "thumb": "down"},
                {"kind": "episode", "file": NANNY, "thumb": "up"},
                {"kind": "unknown", "thumb": "up"}]
        self.assertEqual(core.latest_thumbs(recs), {"spot:a.mp4": "down", "file:" + NANNY: "up"})


class EpisodeInfoTests(unittest.TestCase):
    def test_public_episode_info(self):
        self.assertEqual(core.episode_info(NANNY), ("S01E09", "Personal Business"))
        self.assertEqual(core.episode_info("/x/Community.S01E08.mkv"), ("S01E08", None))


class RemoteAppClassifyTests(unittest.TestCase):
    def test_remote_app_is_rated_as_the_service(self):
        st = {"channel_number": 9, "network_name": "WDBS STREAM", "title": "WDBS STREAM", "content_type": "remote_app"}
        self.assertEqual(core.classify(st, None)["kind"], "app")
        self.assertEqual(core.content_key(st, None), "app:9")
        rec = core.rating_record(rid="r", thumb="up", reason=None, status=st, now_local=datetime(2026, 9, 28, 21, 0),
                                 slate=None, sched=None, log_lines=[])
        self.assertEqual(rec["label"], "WDBS STREAM (interactive)")


class KeyRepeatTests(unittest.TestCase):
    def test_navigation_keys_are_not_swallowed(self):
        g = core.Guard()
        g.ran(10.0, "key:ArrowRight")
        self.assertIsNone(g.check(10.2, "key:ArrowRight"))          # quick second ▶ counts (was dropped < 0.6 s)
        self.assertEqual(g.check(10.05, "key:ArrowRight"), "repeat")  # a phone double-fire is still filtered
        g.ran(15.0, "vol_up")
        self.assertIsNone(g.check(15.15, "vol_up"))                 # rapid VOL+ presses all count
        g.ran(16.0, "mute")
        self.assertEqual(g.check(16.3, "mute"), "repeat")           # MUTE keeps the guard
        g.ran(20.0, "ch_up")
        self.assertEqual(g.check(20.3, "ch_up"), "repeat")          # channel commands keep the 0.6 s guard


class ThumbsToPageTest(unittest.TestCase):
    """Ch 5 WDBS RADIO takes 👍/👎 as keys (rates the MilkDrop look); other channels keep rating records."""

    def test_radio_page_takes_thumbs(self):
        self.assertTrue(core.thumbs_to_page({"network_name": "WDBS RADIO", "channel_number": 5}))
        self.assertFalse(core.thumbs_to_page({"network_name": "EXAMPLE SITCOMS", "channel_number": 10}))
        self.assertFalse(core.thumbs_to_page({}))

    def test_thumb_and_media_keys_parse(self):
        for k in ("ThumbsUp", "ThumbsDown", "PlayPause", "FastForward", "Rewind"):
            self.assertEqual(core.parse_command(f"key:{k}"), ("key", k))
