"""Volume meter pure logic (wdbs/volume_core.py)."""

import unittest

from wdbs import volume_core as vc

VOL = "Volume: front-left: 49107 /  75% / -7.52 dB,   front-right: 49107 /  75% / -7.52 dB\n        balance 0.00"


class ParseTests(unittest.TestCase):
    def test_volume_and_mute(self):
        self.assertEqual(vc.parse_reading(VOL, "Mute: no"), (75, False))
        self.assertEqual(vc.parse_reading(VOL, "Mute: yes"), (75, True))

    def test_loudest_channel_wins(self):
        self.assertEqual(
            vc.parse_reading("Volume: front-left: 1 /  40% / x, front-right: 2 /  60% / y", "Mute: no"), (60, False))

    def test_unreadable(self):
        self.assertIsNone(vc.parse_reading("", "Mute: no"))
        self.assertIsNone(vc.parse_reading("Connection failure", ""))

    def test_lit_segments(self):
        self.assertEqual(vc.lit_segments(0), 0)
        self.assertEqual(vc.lit_segments(100), vc.SEGMENTS)
        self.assertEqual(vc.lit_segments(150), vc.SEGMENTS)       # over-amplified reads as full
        self.assertEqual(vc.lit_segments(-5), 0)
        self.assertEqual(vc.lit_segments(50), vc.SEGMENTS // 2)

    def test_sink_events(self):
        self.assertTrue(vc.is_sink_event("Event 'change' on sink #53"))
        self.assertTrue(vc.is_sink_event("Event 'change' on server"))
        self.assertFalse(vc.is_sink_event("Event 'change' on sink-input #12"))
        self.assertFalse(vc.is_sink_event("Event 'remove' on sink #53"))
        self.assertFalse(vc.is_sink_event(""))


class MeterTests(unittest.TestCase):
    def test_first_reading_is_only_a_baseline(self):
        m = vc.Meter()
        self.assertFalse(m.observe((75, False), 0.0))
        self.assertFalse(m.visible)
        self.assertEqual(m.state(0.1), (0.0, 0.0))

    def test_change_shows_and_same_reading_does_not(self):
        m = vc.Meter()
        m.observe((75, False), 0.0)
        self.assertFalse(m.observe((75, False), 1.0))
        self.assertTrue(m.observe((80, False), 2.0))
        self.assertTrue(m.visible)
        self.assertTrue(m.observe((80, True), 2.5))               # mute toggled
        self.assertFalse(m.observe(None, 3.0))                    # unreadable never shows or clears

    def test_timeline_slide_linger_fade_idle(self):
        m = vc.Meter()
        m.observe((50, False), 0.0)
        m.observe((55, False), 10.0)
        self.assertEqual(m.state(10.0), (1.0, 0.0))
        self.assertEqual(m.state(10.0 + vc.SLIDE_S), (1.0, 1.0))
        self.assertEqual(m.state(10.0 + vc.LINGER_S - 0.01), (1.0, 1.0))
        a, s = m.state(10.0 + vc.LINGER_S + vc.FADE_S / 2)
        self.assertTrue(0.4 < a < 0.6)
        self.assertEqual(s, 1.0)
        self.assertEqual(m.state(10.0 + vc.LINGER_S + vc.FADE_S + 0.01), (0.0, 0.0))
        self.assertFalse(m.visible)

    def test_new_change_restarts_the_timeline(self):
        m = vc.Meter()
        m.observe((50, False), 0.0)
        m.observe((55, False), 1.0)
        m.observe((60, False), 3.0)                               # the first change's fade would start at 3.5
        self.assertEqual(m.state(3.0 + vc.LINGER_S - 0.01)[0], 1.0)


if __name__ == "__main__":
    unittest.main()
