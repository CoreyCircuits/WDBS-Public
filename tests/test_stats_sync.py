import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "wdbs_stats_sync", Path(__file__).resolve().parents[1] / "prodesk" / "wdbs_stats_sync.py")
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)


def page(**props):
    out = {}
    for name, (typ, value) in props.items():
        if typ == "date":
            out[name] = {"type": "date", "date": {"start": value} if value else None}
        elif typ == "number":
            out[name] = {"type": "number", "number": value}
        else:
            out[name] = {"type": "rich_text", "rich_text": [{"plain_text": value}] if value else []}
    return {"id": "p1", "properties": out}


HIST = {"tag": "martin", "last_aired": "2026-09-30 13:01:36", "first_aired": "2026-09-30 01:00:05",
        "airings_7d": 5, "airings_30d": 5, "airings_total": 5, "unique_episodes_aired": 4,
        "episodes_in_library": 132, "unplayed_episodes": 128, "last_episode_aired": "S01E03 Martin",
        "next_episode": "Martin - S01E04", "next_scheduled_at": "2026-09-30 21:30:00"}


class StatsSyncTests(unittest.TestCase):
    def test_local_times_become_minute_precision_with_offset(self):
        self.assertEqual(ss.to_minute("2026-09-30 13:01:36"), "2026-09-30T13:01:00-04:00")
        self.assertIsNone(ss.to_minute(None))

    def test_blank_row_gets_everything(self):
        p = page(**{n: ("date", None) for n in ss.DATES}, **{n: ("number", None) for n in ss.NUMBERS},
                 **{n: ("rich_text", None) for n in ss.TEXTS})
        body = ss.patch_body(p, ss.wanted(HIST))
        self.assertEqual(set(body), set(ss.DATES) | set(ss.NUMBERS) | set(ss.TEXTS))
        self.assertEqual(body["Last Aired"], {"date": {"start": "2026-09-30T13:01:00-04:00"}})

    def test_unchanged_row_sends_nothing(self):
        want = ss.wanted(HIST)
        p = page(**{n: (t, v) for n, (t, v) in want.items()})
        self.assertEqual(ss.patch_body(p, want), {})

    def test_hand_set_first_air_is_never_overwritten_or_cleared(self):
        p = page(**{"First WDBS Air": ("date", "2026-09-25")})
        self.assertEqual(ss.patch_body(p, ss.wanted(HIST)), {})
        self.assertEqual(ss.patch_body(p, ss.wanted(dict(HIST, first_aired=None))), {})

    def test_last_aired_is_not_cleared_when_the_tv_host_has_none(self):
        p = page(**{"Last Aired": ("date", "2026-09-29T20:00:00.000-04:00"), "Last Episode Aired": ("rich_text", "x")})
        self.assertEqual(ss.patch_body(p, ss.wanted(dict(HIST, last_aired=None, last_episode_aired=None))), {})

    def test_next_scheduled_can_clear(self):
        p = page(**{"Next Scheduled": ("date", "2026-09-30T21:30:00.000-04:00")})
        self.assertEqual(ss.patch_body(p, ss.wanted(dict(HIST, next_scheduled_at=None))), {"Next Scheduled": {"date": None}})

    def test_unknown_library_does_not_claim_zero_unplayed(self):
        self.assertNotIn("Unplayed Episodes", ss.wanted(dict(HIST, episodes_in_library=0, unplayed_episodes=0)))


if __name__ == "__main__":
    unittest.main()
