import unittest

from wdbs.playout_stats import player_counts, slate_durations

P = "2026-09-30T12:59:{:02d}-04:00 TV_HOST python[1]: 2026-09-30 12:59:00,000 "


class PlayoutStatsTests(unittest.TestCase):
    def test_player_counts_split_ads_and_episodes(self):
        lines = [P.format(1) + "INFO:FieldPlayer:playing /c/example_sitcoms/martin/S1/a.mkv",
                 P.format(2) + "INFO:FieldPlayer:playing /c/example_sitcoms/commercial/x.mp4",
                 P.format(3) + "ERROR:FieldPlayer:Timeout waiting for playback to start on /c/example_sitcoms/commercial/x.mp4",
                 P.format(4) + "ERROR:FieldPlayer:Error waiting for playback: No response from MPV."]
        c = player_counts(lines)
        self.assertEqual((c["plays"], c["ad_plays"], c["start_timeouts"], c["ad_timeouts"], c["episode_timeouts"],
                          c["no_response_from_mpv"], c["timeout_rate"]), (2, 1, 1, 1, 0, 1, 0.5))

    def test_slate_pairs(self):
        lines = ["2026-09-30T12:59:34-04:00 w python[2]: x INFO wdbs.slate: show start (success) over ad",
                 "2026-09-30T13:02:18-04:00 w python[2]: x INFO wdbs.slate: hide",
                 "2026-09-30T13:05:00-04:00 w python[2]: x INFO wdbs.slate: show stall over ep",
                 "2026-09-30T13:05:03-04:00 w python[2]: x INFO wdbs.slate: hide"]
        s = slate_durations(lines)
        self.assertEqual(s["start"]["max_s"], 164.0)
        self.assertEqual(s["stall"]["n"], 1)

    def test_empty_window(self):
        self.assertIsNone(player_counts([])["timeout_rate"])
        self.assertEqual(slate_durations([]), {})
