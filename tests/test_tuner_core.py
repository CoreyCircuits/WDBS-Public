"""Tuning transition pure logic (wdbs/tuner_core.py)."""

import datetime as dt
import unittest
from types import SimpleNamespace as NS

from wdbs import tuner_core as tc

CAT = "/path/to/FieldStation42/catalog/example_sitcoms"
NANNY = f"{CAT}/nanny/Season 1/The Nanny - S01E14 - The Family Plumbing.mkv"
COMM = f"{CAT}/community/Season 1/Community.S01E13.720p.BluRay.x264-YELLOWBiRD.mkv"
PROGRAMS = [{"tag": "nanny", "program": "The Nanny", "premiere_year": 1993, "era_override": None},
            {"tag": "community", "program": "Community", "premiere_year": 2009, "era_override": None},
            {"tag": "sanford_and_son", "program": "Sanford and Son", "premiere_year": 1972, "era_override": None}]


def block(path, h1, m1, h2, m2, title="raw.file.title"):
    d = dt.datetime(2026, 9, 28)
    plan = [{"path": "/x/commercial/a.mp4", "content_type": "commercial", "duration": 30},
            {"path": path, "content_type": "feature", "duration": 600}]
    return NS(start=d.replace(hour=h1, minute=m1), end=d.replace(hour=h2, minute=m2), title=title,
              plan_paths=lambda: plan)


class ShowIndexTests(unittest.TestCase):
    def test_names_and_eras_from_the_manifest(self):
        shows = tc.show_index(PROGRAMS)
        self.assertEqual(shows["nanny"], tc.Show("The Nanny", "90s"))
        self.assertEqual(shows["sanford_and_son"].era, "70s")
        self.assertEqual(tc.show_index([{"tag": "", "program": "x"}]), {})


class NetworkNameTests(unittest.TestCase):
    def test_known_and_legacy_names(self):
        self.assertEqual(tc.network_name(10, "EXAMPLE SITCOMS"), "WDBS SITCOMS")
        self.assertEqual(tc.network_name(4, "WEATHERCAST WEATHER"), "WDBS WEATHER")
        self.assertEqual(tc.network_name(5, "WDBS RADIO"), "WDBS RADIO")
        self.assertEqual(tc.network_name(42, "TV_HOST MOVIES"), "WDBS MOVIES")
        self.assertEqual(tc.network_name(7, None), "CHANNEL 7")


class CardTests(unittest.TestCase):
    def test_channel_10_episode(self):
        c = tc.card_for(10, "EXAMPLE SITCOMS", "standard", CAT, block(NANNY, 16, 30, 17, 0),
                        block(COMM, 17, 0, 17, 30), tc.show_index(PROGRAMS))
        self.assertEqual(c.era, "90s")
        self.assertEqual(c.banner, tc.Banner(10, "WDBS SITCOMS", "The Nanny",
                                             "S01E14 · The Family Plumbing · 4:30–5:00", "Next 5:00 · Community"))

    def test_scene_release_names_and_unknown_shows(self):
        c = tc.card_for(10, "EXAMPLE SITCOMS", "standard", CAT, block(COMM, 18, 0, 18, 30), None, tc.show_index(PROGRAMS))
        self.assertEqual((c.banner.title, c.banner.detail, c.banner.next, c.era), ("Community", "S01E13 · 6:00–6:30", "", "00s"))
        stray = block(f"{CAT}/mystery/Mystery.S02E01.mkv", 18, 0, 18, 30, title="Mystery.S02E01")
        c = tc.card_for(10, "EXAMPLE SITCOMS", "standard", CAT, stray, None, tc.show_index(PROGRAMS))
        self.assertEqual((c.banner.title, c.era), ("Mystery", "00s"))

    def test_web_channels(self):
        c = tc.card_for(3, "WDBS TODAY", "web", None, None, None, {})
        self.assertEqual((c.banner, c.era), (tc.Banner(3, "WDBS TODAY", "Your day, live"), "00s"))
        self.assertEqual(tc.card_for(99, "TV_HOST GUIDE", "web", None, None, None, {}).banner.title, "What's on WDBS")
        self.assertEqual(tc.card_for(5, "WDBS RADIO", "web", None, None, None, {}).banner.title, "")

    def test_no_schedule_shows_number_and_name(self):
        c = tc.card_for(10, "EXAMPLE SITCOMS", "standard", CAT, None, None, {})
        self.assertEqual((c.banner.channel, c.banner.network, c.banner.title, c.era), (10, "WDBS SITCOMS", "", "00s"))

    def test_next_block_title(self):
        shows = tc.show_index(PROGRAMS)
        c = tc.card_for(10, "EXAMPLE SITCOMS", "standard", CAT, block(COMM, 16, 30, 17, 0),
                        block(f"{CAT}/sanford_and_son/S1/Sanford.and.Son.S01E02.mkv", 17, 0, 17, 30), shows)
        self.assertEqual(c.banner.next, "Next 5:00 · Sanford and Son")


B10 = tc.Banner(10, "WDBS SITCOMS", "The Nanny")
B99 = tc.Banner(99, "WDBS GUIDE", "What's on WDBS")
C10, C99 = tc.Card(B10, "90s"), tc.Card(B99, "00s")
GUIDE_URL = "http://localhost:4242/static/wdbsguide/index.html"


class MoveWatchTests(unittest.TestCase):
    def test_moving_needs_the_same_real_file_to_advance(self):
        w = tc.MoveWatch()
        self.assertFalse(w.update("/x/a.mkv", 10.0))
        self.assertFalse(w.update("/x/a.mkv", 10.1))
        self.assertTrue(w.update("/x/a.mkv", 10.4))

    def test_standby_black_and_web_placeholders_never_count(self):
        for p in ("/path/to/FieldStation42/runtime/standby.png", "av://lavfi:color=c=black", ":autobump:=http://x"):
            w = tc.MoveWatch()
            w.update(p, 0.0)
            self.assertFalse(w.update(p, 5.0), p)

    def test_a_new_file_restarts_the_watch(self):
        w = tc.MoveWatch()
        w.update("/x/a.mkv", 10.0)
        self.assertFalse(w.update("/x/b.mkv", 10.5))
        self.assertTrue(w.update("/x/b.mkv", 10.9))

    def test_fs42_seek_jump_is_not_moving(self):
        w = tc.MoveWatch()               # play(file) at 0.0, then seek to the slot offset
        for t in (0.0, 983.39, 983.39, 983.5):
            self.assertFalse(w.update("/x/a.mkv", t), t)
        self.assertTrue(w.update("/x/a.mkv", 983.7))

    def test_a_rewind_resets_the_progress(self):
        w = tc.MoveWatch()
        w.update("/x/a.mkv", 10.0)
        self.assertFalse(w.update("/x/a.mkv", 10.2))
        self.assertFalse(w.update("/x/a.mkv", 5.0))
        self.assertFalse(w.update("/x/a.mkv", 5.2))
        self.assertTrue(w.update("/x/a.mkv", 5.35))

    def test_the_old_channel_never_counts(self):
        # 90 -> 10: the old file keeps playing until FS42 writes the new channel's status, then loads the new file
        w = tc.MoveWatch()
        for t in (100.0, 100.1, 100.2, 100.3, 100.4):
            self.assertFalse(tc.watch_step(w, 90, 10, "/x/old.mkv", t), t)
        self.assertFalse(tc.watch_step(w, 10, 10, "/x/old.mkv", 100.5))     # status flipped, mpv not loaded yet
        for t in (0.0, 491.2, 491.3, 491.4):
            self.assertFalse(tc.watch_step(w, 10, 10, "/x/new.mkv", t), t)
        self.assertTrue(tc.watch_step(w, 10, 10, "/x/new.mkv", 491.55))

    def test_an_unknown_status_never_blocks(self):
        w = tc.MoveWatch()
        for t in (10.0, 10.1, 10.2):
            self.assertFalse(tc.watch_step(w, None, 10, "/x/a.mkv", t))
        self.assertTrue(tc.watch_step(w, None, 10, "/x/a.mkv", 10.35))


class TunerTests(unittest.TestCase):
    def test_idle_shows_nothing(self):
        self.assertEqual(tc.Tuner().tick(0.0), tc.Display(None, None, False))

    def test_tune_holds_until_the_picture_then_lingers(self):
        t = tc.Tuner()
        t.tune(0.0, C10, None)
        self.assertEqual(t.tick(0.5), tc.Display("90s", B10, True))
        t.picture(1.0)
        self.assertEqual(t.tick(1.1), tc.Display(None, B10, False))
        self.assertEqual(t.tick(1.0 + tc.LINGER_S + 0.01), tc.Display(None, None, False))

    def test_web_channel_waits_for_its_page_plus_paint_time(self):
        t = tc.Tuner()
        t.tune(0.0, C99, GUIDE_URL)
        t.picture(0.5)                                   # mpv black frames don't count for a web channel
        self.assertTrue(t.tick(0.6).holding)
        t.web_ready(2.0, GUIDE_URL)
        self.assertTrue(t.tick(2.2).holding)
        self.assertEqual(t.tick(2.0 + tc.WEB_PAINT_S), tc.Display(None, B99, False))
        self.assertEqual(t.tick(2.0 + tc.WEB_PAINT_S + tc.GUIDE_LINGER_S + 0.01).banner, None)

    def test_web_ready_without_a_tune_does_nothing(self):
        t = tc.Tuner()
        t.web_ready(1.0, "http://app-host.example.invalid:9090/index.html?x=1")
        self.assertEqual(t.tick(1.5), tc.Display(None, None, False))

    def test_web_ready_for_another_page_is_ignored(self):
        t = tc.Tuner()
        t.tune(0.0, C99, GUIDE_URL)
        t.web_ready(1.0, "http://app-host.example.invalid:9090/index.html?duration=56000")   # a WeatherStar prewarm
        self.assertTrue(t.tick(2.0).holding)

    def test_web_ready_with_another_query_string_is_ignored(self):
        t = tc.Tuner()
        t.tune(0.0, C99, GUIDE_URL)
        t.web_ready(1.0, GUIDE_URL + "?x=1")
        self.assertTrue(t.tick(2.0).holding)

    def test_standard_tune_ignores_web_ready_until_a_web_entry_is_expected(self):
        t = tc.Tuner()
        t.tune(0.0, C10, None)
        t.web_ready(1.0, "http://app-host.example.invalid:9090/index.html?duration=56000")
        self.assertTrue(t.tick(1.0 + tc.WEB_PAINT_S + 0.1).holding)

    def test_weather_break_on_a_standard_channel_waits_for_its_page(self):
        t = tc.Tuner()
        t.tune(0.0, C10, None)
        t.expect_web(0.8)                                # FS42's status: Ch 10 is playing a web entry
        self.assertTrue(t.tick(1.5).holding)
        t.web_ready(2.0, "http://app-host.example.invalid:9090/index.html?duration=56000")
        self.assertTrue(t.tick(2.0 + tc.WEB_PAINT_S - 0.01).holding)
        self.assertEqual(t.tick(2.0 + tc.WEB_PAINT_S), tc.Display(None, B10, False))

    def test_web_ready_between_the_tune_and_expect_web_counts(self):
        t = tc.Tuner()
        t.tune(0.0, C10, None)
        t.web_ready(0.5, "http://app-host.example.invalid:9090/index.html?duration=56000")
        t.expect_web(0.8)
        self.assertEqual(t.tick(0.5 + tc.WEB_PAINT_S), tc.Display(None, B10, False))

    def test_web_ready_before_the_tune_does_not_count(self):
        t = tc.Tuner()
        t.web_ready(0.5, "http://x/")
        t.tune(1.0, C10, None)
        t.expect_web(1.2)
        self.assertTrue(t.tick(2.0).holding)

    def test_surfing_keeps_one_card_and_the_newest_channel(self):
        t = tc.Tuner()
        t.tune(0.0, C10, None)
        t.tune(0.4, tc.Card(tc.Banner(3, "WDBS TODAY", "Your day, live"), "00s"), "http://app-host.example.invalid:8742/?kiosk")
        d = t.tick(0.5)
        self.assertEqual((d.backdrop, d.banner.channel, d.holding), ("00s", 3, True))
        t.tune(0.8, C10, None)
        self.assertEqual(t.tick(0.9), tc.Display("90s", B10, True))
        t.picture(1.2)
        self.assertEqual(t.tick(1.3).banner, B10)

    def test_hold_hands_over_after_30_s(self):
        t = tc.Tuner()
        t.tune(0.0, C10, None)
        self.assertTrue(t.tick(tc.HANDOVER_S - 0.1).holding)
        self.assertEqual(t.tick(tc.HANDOVER_S), tc.Display(None, None, False))

    def test_info_shows_the_banner_alone_and_waits_for_a_tune(self):
        t = tc.Tuner()
        t.info(5.0, C10)
        self.assertEqual(t.tick(5.1), tc.Display(None, B10, False))
        self.assertEqual(t.tick(5.0 + tc.INFO_S + 0.01), tc.Display(None, None, False))
        t.tune(20.0, C10, None)
        t.info(20.5, C99)                                 # INFO during a tune changes nothing
        self.assertEqual(t.tick(20.6), tc.Display("90s", B10, True))

    def test_late_schedule_answer_updates_the_banner(self):
        t = tc.Tuner()
        t.tune(0.0, tc.Card(tc.Banner(10, "WDBS SITCOMS", ""), "00s"), None)
        t.update_card(C10)
        self.assertEqual(t.tick(0.2), tc.Display("90s", B10, True))
        t.update_card(C99)                                # a card for another channel is stale: ignored
        self.assertEqual(t.tick(0.3).banner, B10)


class HoldingNowTests(unittest.TestCase):
    def test_fresh_holding(self):
        self.assertTrue(tc.holding_now({"holding": True, "since": 100.0}, 110.0))

    def test_stale_holding_is_ignored(self):
        self.assertFalse(tc.holding_now({"holding": True, "since": 100.0}, 100.0 + tc.HOLD_FRESH_S))

    def test_not_holding_or_unreadable(self):
        for s in ({"holding": False, "since": 100.0}, None, {}, {"holding": True}, {"holding": True, "since": "x"}, True, 42, [1, 2], "x"):
            self.assertFalse(tc.holding_now(s, 101.0), s)


class RemoteAppHoldTests(unittest.TestCase):
    """Channel Router: a remote-app (executable) tune holds until fs42/wdbs_remote_app.py signals wdbs-app:<ch>."""

    def test_hold_url(self):
        self.assertEqual(tc.hold_url(3, "web", "http://x/"), "http://x/")
        self.assertEqual(tc.hold_url(9, "executable", None), "wdbs-app:9")
        self.assertIsNone(tc.hold_url(10, "standard", None))

    def test_app_signal_releases_card(self):
        t = tc.Tuner()
        card = tc.card_for(9, "WDBS STREAM", "executable", None, None, None, {})
        self.assertEqual((card.banner.network, card.banner.title), ("WDBS STREAM", "Interactive · Powered by Stremio"))
        t.tune(0.0, card, tc.hold_url(9, "executable", None))
        t.web_ready(1.0, "http://app-host.example.invalid:9090/index.html")      # some other page: not ours
        self.assertTrue(t.tick(2.0).holding)
        t.web_ready(3.0, "wdbs-app:9")
        self.assertTrue(t.tick(3.2).holding)
        self.assertFalse(t.tick(3.0 + tc.WEB_PAINT_S).holding)


class AppCardTests(unittest.TestCase):
    def test_app_card_shows_what_is_playing(self):
        st = NS(channel=9, title="Atlanta · S2 E6 · Teddy Perkins")
        c = tc.app_card(9, "WDBS STREAM", st)
        self.assertEqual((c.banner.title, c.banner.detail), ("Atlanta · S2 E6 · Teddy Perkins", "WDBS Stream · Interactive"))
        self.assertEqual(tc.app_card(9, "WDBS STREAM", NS(channel=9, title="WDBS STREAM")).banner.title,
                         "Interactive · Powered by Stremio")                  # browsing: tagline
        self.assertEqual(tc.app_card(9, "WDBS STREAM", NS(channel=10, title="Seinfeld")).banner.title,
                         "Interactive · Powered by Stremio")                  # stale status from another channel
        self.assertEqual(tc.app_card(9, "WDBS STREAM", None).banner.title, "Interactive · Powered by Stremio")
