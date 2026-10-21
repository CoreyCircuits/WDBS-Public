import datetime as dt
import json
import logging
import os
import unittest
from unittest import mock

from wdbs import fs42_adapter as fs
from wdbs import replan as rp
from wdbs.api import ControlPlane
from wdbs.episodes import parse_episode
from wdbs.policy import (PolicyError, ProgramFacts, ProgramPolicy, Slot, build_lineup, daypart,
                         halfhour_slots, hourly_slots)
from wdbs.recorder import Recorder
from wdbs.state import StateStore
from wdbs.timeparse import parse_when

from tests.fixture import STATION, FakeFS42, build_blocks

DAY0 = dt.datetime(2026, 9, 26)
logging.getLogger("wdbs").setLevel(logging.ERROR)


class EpisodeTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_episode("/x/Seinfeld - S01E03 - The Robbery.mkv").code, "S01E03")
        self.assertEqual(parse_episode("Community.S01E12.720p.BluRay.x264.mkv").code, "S01E12")
        self.assertEqual(parse_episode("Sanford and Son - 1x07 - A Pad for Lamont.avi").code, "S01E07")
        self.assertIsNone(parse_episode("EXAMPLE_SITCOMS_ID.mp4").code)
        self.assertIsNone(parse_episode("Car Insurance [kHQVNVgVzD8].mp4").code)


class SequenceCursorTests(unittest.TestCase):
    def seq(self, cursor):
        return fs.Sequence(1, "S", "bmw", "bmw", 0.0, 1.0, cursor, None, [f"e{i:02d}" for i in range(22)])

    def test_follows_after_last_episode_wraps(self):
        # Last kept airing is E22 (idx 21): FS42 treats cursor 22 and cursor 0 alike.
        self.assertTrue(self.seq(22).follows(21))
        self.assertTrue(self.seq(0).follows(21))
        self.assertFalse(self.seq(1).follows(21))

    def test_follows_mid_sequence_is_exact(self):
        self.assertTrue(self.seq(5).follows(4))
        self.assertFalse(self.seq(0).follows(4))
        self.assertFalse(self.seq(6).follows(4))


class TimeParseTests(unittest.TestCase):
    def test_parse(self):
        now = dt.datetime(2026, 9, 26, 15, 10)
        self.assertEqual(parse_when("tomorrow 06:00", now), dt.datetime(2026, 9, 27, 6, 0))
        self.assertEqual(parse_when("+2h", now), dt.datetime(2026, 9, 26, 17, 10))
        self.assertEqual(parse_when("14:00", now), dt.datetime(2026, 9, 27, 14, 0))
        self.assertEqual(parse_when("2026-09-28 06:30", now), dt.datetime(2026, 9, 28, 6, 30))
        with self.assertRaises(ValueError):
            parse_when("whenever", now)


class StatusParseTests(unittest.TestCase):
    def test_status(self):
        st = fs.parse_status({"status": "playing", "network_name": "EXAMPLE SITCOMS", "channel_number": 10,
                              "timestamp": "2026-09-26T15:00:30", "title": "X", "duration": "0:00:09/0:04:03",
                              "file_path": "/a/b.mkv", "content_type": "feature"})
        self.assertEqual((st.offset_s, st.segment_s, st.channel), (9.0, 243.0, 10))
        self.assertEqual(st.timestamp, dt.datetime(2026, 9, 26, 15, 0, 30))
        self.assertIsNone(fs.parse_status({"status": "stopped", "channel_number": -1}).channel)


def P(tag, dayparts=(), **kw):
    return ProgramPolicy(tag=tag, program=tag.title(), dayparts=list(dayparts), **kw)


class PolicyTests(unittest.TestCase):
    def slots(self, h0, n):
        return hourly_slots(DAY0 + dt.timedelta(hours=h0), DAY0 + dt.timedelta(hours=h0 + n))

    def test_daypart_is_a_hard_gate_when_satisfiable(self):
        progs = [P("a", ["Prime"]), P("b", ["Morning"]), P("c", ["Prime"])]
        out = build_lineup(progs, {}, self.slots(18, 4))
        self.assertTrue(all(d.tag in ("a", "c") for d in out))
        self.assertTrue(all(not d.relaxed for d in out))
        self.assertIn("Ineligible: B (not programmed for Prime", out[0].reason)

    def test_relaxes_daypart_with_explanation(self):
        progs = [P("a", ["Prime"]), P("b", ["Morning"])]
        out = build_lineup(progs, {}, self.slots(0, 3))
        self.assertEqual(out[0].relaxed, ["daypart"])
        self.assertIn("Relaxed daypart", out[0].reason)

    def test_never_back_to_back_when_avoidable_and_balances_by_weight(self):
        progs = [P("a", weight=2.0), P("b"), P("c")]
        out = build_lineup(progs, {}, self.slots(0, 24))
        tags = [d.tag for d in out]
        self.assertTrue(all(x != y for x, y in zip(tags, tags[1:])))
        self.assertGreater(tags.count("a"), tags.count("b"))

    def test_non_relaxable_gates_are_never_bypassed(self):
        progs = [P("a"), P("b", in_rotation=False, inactive_reason="deactivated by operator")]
        facts = {"a": ProgramFacts(), "b": ProgramFacts()}
        out = build_lineup(progs, facts, self.slots(8, 3))
        self.assertEqual([d.tag for d in out], ["a", "a", "a"])
        self.assertIn("back_to_back", out[1].relaxed)
        with self.assertRaises(PolicyError):
            build_lineup([P("a")], {"a": ProgramFacts(media_available=False)}, self.slots(8, 1))

    def test_max_per_day(self):
        progs = [P("a", max_per_day=1, weight=5), P("b")]
        out = build_lineup(progs, {}, self.slots(8, 3))
        self.assertEqual([d.tag for d in out].count("a"), 1)

    def test_capped_program_keeps_its_slot_for_its_dayparts(self):
        progs = [P("a", ["Prime"], max_per_day=1, weight=3), P("b", ["Daytime"]), P("c", ["Daytime"])]
        out = build_lineup(progs, {}, self.slots(0, 24))
        a_slots = [d.hour for d in out if d.tag == "a"]
        self.assertEqual(len(a_slots), 1)
        self.assertIn(a_slots[0], range(18, 22))

    def test_locked_slot(self):
        progs = [P("a"), P("b")]
        start = DAY0 + dt.timedelta(hours=9)
        slots = hourly_slots(start, start + dt.timedelta(hours=2), {start: ("b", "test lock")})
        out = build_lineup(progs, {}, slots)
        self.assertEqual(out[0].tag, "b")
        self.assertIn("locked", out[0].reason)

    def test_scoring_inputs_are_explained(self):
        progs = [P("a", manual_boost=1.5, imdb_rating=8.5, imdb_votes=100000, wdbs_rating=9), P("b")]
        facts = {"a": ProgramFacts(unplayed_episodes=10, airings_7d=4), "b": ProgramFacts(unplayed_episodes=0)}
        d = build_lineup(progs, facts, self.slots(12, 1))[0]
        self.assertEqual(d.tag, "a")
        names = {c[0] for c in d.components}
        self.assertTrue({"manual_boost", "imdb", "wdbs_rating", "inventory", "exposure_7d"} <= names)
        self.assertIn("Manual Boost +1.5", d.reason)

    def test_deterministic(self):
        progs = [P("a"), P("b"), P("c")]
        a = [d.tag for d in build_lineup(progs, {}, self.slots(0, 24))]
        b = [d.tag for d in build_lineup(progs, {}, self.slots(0, 24))]
        self.assertEqual(a, b)

    def test_dayparts(self):
        self.assertEqual([daypart(h) for h in (0, 5, 7, 11, 15, 18, 22)],
                         ["Overnight", "Early", "Morning", "Daytime", "After School", "Prime", "Late Night"])


class HalfHourTests(unittest.TestCase):
    def slots(self, h0, hours, days=0):
        start = DAY0 + dt.timedelta(days=days, hours=h0)
        return halfhour_slots(start, start + dt.timedelta(hours=hours))

    def test_slots_start_on_the_half_hour_grid(self):
        s = halfhour_slots(DAY0 + dt.timedelta(hours=6, minutes=40), DAY0 + dt.timedelta(hours=8))
        self.assertEqual([x.start.strftime("%H:%M") for x in s], ["06:30", "07:00", "07:30"])
        self.assertTrue(all(x.length_h == 0.5 for x in s))

    def test_max_per_day_counts_hours_not_slots(self):
        progs = [P("a", max_per_day=1, weight=5), P("b"), P("c")]
        out = build_lineup(progs, {}, self.slots(8, 4))
        self.assertEqual([d.tag for d in out].count("a"), 2)  # 1 hour a day = two half-hours

    def test_adjacent_halves_differ_when_possible(self):
        out = [d.tag for d in build_lineup([P("a"), P("b"), P("c")], {}, self.slots(0, 24))]
        self.assertTrue(all(x != y for x, y in zip(out, out[1:])))

    def test_lineup_rotates_day_to_day(self):
        progs = [P(t) for t in "abcdefgh"]
        out = [d.tag for d in build_lineup(progs, {}, self.slots(0, 72))]
        day1, day2, day3 = out[:48], out[48:96], out[96:]
        self.assertLess(sum(x == y for x, y in zip(day1, day2)), 48 * 0.2)
        self.assertLess(sum(x == y for x, y in zip(day2, day3)), 48 * 0.2)
        from wdbs import policy
        with mock.patch.object(policy, "W_SAME_TIME", (0.0, 0.0, 0.0)):
            flat = [d.tag for d in build_lineup(progs, {}, self.slots(0, 48))]
        self.assertGreater(sum(x == y for x, y in zip(flat[:48], flat[48:])),
                           sum(x == y for x, y in zip(day1, day2)))  # the rotation penalty does the work

    def test_prior_airings_seed_rotation(self):
        progs = [P("a"), P("b"), P("c")]
        y = DAY0 - dt.timedelta(days=1)
        prior = [(y + dt.timedelta(hours=8), "a", 0.5)]
        d = build_lineup(progs, {}, self.slots(8, 0.5), prior=prior)[0]
        self.assertNotEqual(d.tag, "a")

    def test_template_check_reads_split_slots(self):
        from wdbs.api import _slot_sequences
        self.assertEqual(_slot_sequences({"tags": ["a", "b"], "sequence": ["a", "b"]}), [("a", "a"), ("b", "b")])
        self.assertEqual(_slot_sequences({"tags": "a", "sequence": "a"}), [("a", "a")])

    def test_hour_slots_from_lineup(self):
        def d(h, m, tag):
            return {"start": (DAY0 + dt.timedelta(hours=h, minutes=m)).isoformat(sep=" "), "tag": tag}
        got = rp.hour_slots_from_lineup([d(6, 0, "a"), d(6, 30, "b"), d(7, 0, "c"), d(7, 30, "c"), d(8, 30, "e")])
        self.assertEqual(got[(DAY0.date(), 6)], {"tags": ["a", "b"], "sequence": ["a", "b"]})
        self.assertEqual(got[(DAY0.date(), 7)], {"tags": "c", "sequence": "c"})
        self.assertEqual(got[(DAY0.date(), 8)], {"tags": "e", "sequence": "e"})  # cutoff at :30
        self.assertEqual(rp.date_overrides_from_lineup([d(6, 0, "a"), d(6, 30, "b")]),
                         {"September 26": {"6": {"tags": ["a", "b"], "sequence": ["a", "b"]}}})


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.env = FakeFS42(DAY0, days=2)
        self.settings = self.env.settings
        self.store = StateStore(self.settings.state_db)

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def cp(self, now):
        return ControlPlane(self.settings, self.store, clock=lambda: now)

    def station(self):
        return fs.station_for_channel(self.settings, 10)

    def fake_build(self, settings, db_path, plan):
        build_blocks(db_path, self.env.template, fs.parse_ts(plan.effective_cutoff),
                     fs.parse_ts(plan.rebuild_until), plan.date_overrides)
        return {"returncode": 0}


class ReplanPlanTests(FixtureCase):
    def test_cutoff_never_interrupts_current_program(self):
        now = DAY0 + dt.timedelta(hours=10, minutes=10)
        with fs.connect_ro(self.env.db) as c:
            plan = rp.compute_plan(c, self.station(), now=now, cutoff=now - dt.timedelta(hours=1))
        self.assertEqual(plan.effective_cutoff, "2026-09-26 10:30:00")
        self.assertIn("on air now", plan.cutoff_reason)
        self.assertTrue(all(fs.parse_ts(b["start"]) >= fs.parse_ts(plan.effective_cutoff)
                            for b in plan.delete_summary))

    def test_cutoff_inside_future_block_snaps_forward(self):
        now = DAY0 + dt.timedelta(hours=1)
        with fs.connect_ro(self.env.db) as c:
            plan = rp.compute_plan(c, self.station(), now=now, cutoff=DAY0 + dt.timedelta(hours=6, minutes=10))
        self.assertEqual(plan.effective_cutoff, "2026-09-26 06:30:00")

    def test_rollback_targets_first_deleted_episode(self):
        now = DAY0 + dt.timedelta(hours=1)
        with fs.connect_ro(self.env.db) as c:
            plan = rp.compute_plan(c, self.station(), now=now, cutoff=DAY0 + dt.timedelta(days=1, hours=6))
            blocks = fs.get_blocks(c, STATION)
            catalog = fs.get_catalog(c, STATION)
        firsts = {}
        for b in blocks:
            if b.start >= DAY0 + dt.timedelta(days=1, hours=6):
                firsts.setdefault(b.tag_path, catalog[b.content_ids[0]].path)
        self.assertEqual({r.sequence_name: r.restore_path for r in plan.sequence_rollbacks}, firsts)
        self.assertEqual(sum(plan.count_restores.values()), len(plan.delete_block_ids))

    def test_refuses_when_cursor_disagrees_with_schedule(self):
        c = self.env.conn()
        c.execute("UPDATE named_sequence SET current_index = current_index + 1 WHERE sequence_name = 'alpha'")
        c.commit()
        c.close()
        with fs.connect_ro(self.env.db) as c:
            with self.assertRaises(rp.ReplanRefused):
                rp.compute_plan(c, self.station(), now=DAY0, cutoff=DAY0 + dt.timedelta(days=1))

    def test_refuses_past_horizon(self):
        with fs.connect_ro(self.env.db) as c:
            with self.assertRaises(rp.ReplanRefused):
                rp.compute_plan(c, self.station(), now=DAY0, cutoff=DAY0 + dt.timedelta(days=3))


class ReplanExecuteTests(FixtureCase):
    def snapshot_db(self):
        c = self.env.conn()
        rows = {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("liquid_blocks", "catalog_entries", "named_sequence", "sequence_entries")}
        c.close()
        return rows

    def test_same_template_replan_reproduces_identical_content(self):
        now = DAY0 + dt.timedelta(hours=10, minutes=5)
        before = self.snapshot_db()
        with mock.patch.object(rp, "run_fs42_build", self.fake_build), \
                mock.patch.object(rp, "overnight_running", return_value=False):
            r = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6), apply=False)
            self.assertTrue(r["result"]["ok"], r["result"]["checks"])
            self.assertEqual(self.snapshot_db(), before, "dry run must not touch the live DB")
            r = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6), apply=True,
                                    expect_digest=r["digest"])
        self.assertTrue(r["result"]["ok"], r["result"]["checks"])
        after = self.snapshot_db()
        strip = lambda rows: [x[1:] for x in rows]  # ignore autoincrement ids of rebuilt blocks
        self.assertEqual(strip(after["liquid_blocks"]), strip(before["liquid_blocks"]))
        self.assertEqual(after["catalog_entries"], before["catalog_entries"])  # counts restored exactly
        self.assertEqual(after["named_sequence"], before["named_sequence"])
        self.assertEqual(after["sequence_entries"], before["sequence_entries"])
        self.assertTrue(r["result"]["backup"].endswith(".bak"))

    def test_engine_lineup_changes_future_only_and_keeps_episode_order(self):
        now = DAY0 + dt.timedelta(hours=10, minutes=5)
        with mock.patch.object(rp, "run_fs42_build", self.fake_build), \
                mock.patch.object(rp, "overnight_running", return_value=False):
            dry = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6), lineup="engine")
            self.assertTrue(dry["result"]["ok"], dry["result"]["checks"])
            self.assertTrue(all(d["reason"] for d in dry["plan"]["lineup"]))
            r = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6), lineup="engine", apply=True,
                                    expect_digest=dry["digest"])
        self.assertTrue(r["result"]["ok"], r["result"]["checks"])
        cutoff = DAY0 + dt.timedelta(days=1, hours=6)
        wanted = {fs.parse_ts(d["start"]): d["tag"] for d in dry["plan"]["lineup"]}
        with fs.connect_ro(self.env.db) as c:
            blocks = fs.get_blocks(c, STATION)
        for b in blocks:
            if b.start >= cutoff:
                self.assertEqual(b.tag_path, wanted[b.start], b.start)  # half-hour slots, split hours honoured
        prime = [wanted[DAY0 + dt.timedelta(days=1, hours=18, minutes=30 * i)] for i in range(8)]
        self.assertEqual(prime.count("alpha"), 4)  # only Prime show; alternates because back-to-back is gated
        v = self.cp(now).verify(10)
        self.assertTrue(v["ok"], v["checks"])

    def test_failed_verification_restores_backup(self):
        now = DAY0 + dt.timedelta(hours=10, minutes=5)
        before = self.snapshot_db()

        def gappy_build(settings, db_path, plan):
            build_blocks(db_path, self.env.template, fs.parse_ts(plan.effective_cutoff) + dt.timedelta(minutes=30),
                         fs.parse_ts(plan.rebuild_until), None)
            return {"returncode": 0}

        with mock.patch.object(rp, "run_fs42_build", gappy_build), \
                mock.patch.object(rp, "overnight_running", return_value=False):
            dry = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6))
            self.assertFalse(dry["result"]["ok"])
            r = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6), apply=True,
                                    expect_digest=dry["digest"])
        self.assertFalse(r["result"]["ok"])
        self.assertTrue(r["result"]["restored"])
        self.assertEqual(self.snapshot_db(), before)

    def test_apply_requires_matching_digest(self):
        now = DAY0 + dt.timedelta(hours=10)
        with mock.patch.object(rp, "overnight_running", return_value=False):
            with self.assertRaises(rp.ReplanRefused):
                self.cp(now).replan(10, DAY0 + dt.timedelta(days=1), apply=True, expect_digest="nope")

    def test_refuses_while_overnight_runs(self):
        now = DAY0 + dt.timedelta(hours=10)
        with mock.patch.object(rp, "overnight_running", return_value=True):
            with self.assertRaises(rp.ReplanRefused):
                with fs.connect_ro(self.env.db) as c:
                    plan = rp.compute_plan(c, self.station(), now=now, cutoff=DAY0 + dt.timedelta(days=1))
                rp.apply(self.settings, plan, now)


class FakePlayerAPI:
    """Stands in for the patched FS42 API + player: on POST it writes the ack like the player."""

    def __init__(self, env, available=True):
        import http.server
        import threading
        env_ref, avail = env, available

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                from urllib.parse import parse_qs, urlparse
                u = urlparse(self.path)
                if not avail or u.path != "/player/commands/reload_schedule":
                    self.send_response(404)
                    self.end_headers()
                    return
                rid = parse_qs(u.query).get("request_id", [""])[0]
                with fs.connect_ro(env_ref.db) as c:
                    s0, e0 = fs.schedule_extent(c, STATION)
                    n = c.execute("SELECT count(*) FROM liquid_blocks").fetchone()[0]
                (env_ref.root / "runtime" / "schedule_reload.ack").write_text(json.dumps({
                    "request_id": rid, "ok": True, "extents": {STATION: [s0.isoformat(), e0.isoformat()]},
                    "blocks": {STATION: n}, "duration_s": 0.01, "reloaded_at": "x"}))
                body = json.dumps({"status": "queued", "request_id": rid}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.HTTPServer(("127.0.0.1", env.api_port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PlayerReloadTests(FixtureCase):
    def test_apply_then_reload_confirmed(self):
        api = FakePlayerAPI(self.env)
        try:
            now = DAY0 + dt.timedelta(hours=10, minutes=5)
            with mock.patch.object(rp, "run_fs42_build", self.fake_build), \
                    mock.patch.object(rp, "overnight_running", return_value=False):
                dry = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6))
                r = self.cp(now).replan(10, DAY0 + dt.timedelta(days=1, hours=6), apply=True,
                                        expect_digest=dry["digest"])
        finally:
            api.close()
        self.assertTrue(r["result"]["ok"])
        self.assertTrue(r["player_reload"]["ok"], r["player_reload"])
        self.assertIn("confirmed", r["player_note"])
        self.assertEqual(self.store.events("player.reload")[0]["payload"]["ok"], True)

    def test_unpatched_player_reports_clearly_and_keeps_db_change(self):
        api = FakePlayerAPI(self.env, available=False)
        try:
            r = self.cp(DAY0).reload_player(10)
        finally:
            api.close()
        self.assertFalse(r["ok"])
        self.assertFalse(r["available"])
        self.assertIn("predates the reload patch", r["error"])

    def test_no_api_listening(self):
        r = self.cp(DAY0).reload_player(10)
        self.assertFalse(r["ok"])
        self.assertIn("unreachable", r["error"])


class RecorderTests(FixtureCase):
    def feed(self, rec, now, **status):
        base = {"status": "playing", "network_name": STATION, "channel_number": 10, "content_type": "feature"}
        rec.status_fn = lambda: fs.parse_status(base | status)
        return rec.poll(now)

    def path(self, tag, n):
        with fs.connect_ro(self.env.db) as c:
            eps = sorted(e.path for e in fs.get_catalog(c, STATION).values() if e.tag == tag)
        return eps[n]

    def test_airing_lifecycle(self):
        rec = Recorder(self.settings, self.store, pid_fn=lambda: [123])
        t0 = DAY0 + dt.timedelta(hours=0)
        a0 = self.path("alpha", 0)
        self.feed(rec, t0 + dt.timedelta(seconds=2), timestamp=t0.isoformat(), file_path=a0,
                  duration="0:00:00/0:07:00")
        # commercial break inside the episode keeps the airing open
        self.feed(rec, t0 + dt.timedelta(minutes=7, seconds=5), timestamp=(t0 + dt.timedelta(minutes=7)).isoformat(),
                  file_path="/x/commercial.mp4", content_type="commercial", duration="0:00:00/0:00:30")
        self.feed(rec, t0 + dt.timedelta(minutes=21), timestamp=(t0 + dt.timedelta(minutes=8)).isoformat(),
                  file_path=a0, duration="0:07:00/0:15:00")
        # next episode starts: previous closes
        a1 = self.path("alpha", 1)
        r = self.feed(rec, t0 + dt.timedelta(minutes=30, seconds=3),
                      timestamp=(t0 + dt.timedelta(minutes=30)).isoformat(), file_path=a1,
                      duration="0:00:00/0:07:00")
        self.assertEqual(r["action"], "started")
        rows = self.store.conn.execute("SELECT * FROM airings ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["tag"], "alpha")
        self.assertEqual(first["season"], 1)
        self.assertEqual(first["episode"], 1)
        self.assertEqual(first["end_reason"], "next_program")
        self.assertEqual(first["schedule_match"], 1)
        self.assertEqual(first["completed"], 1)
        self.assertIsNone(rows[1]["ended_at"])
        # player goes away: open airing closes
        rec.pid_fn = lambda: []
        r = rec.poll(t0 + dt.timedelta(minutes=35))
        self.assertEqual(r["action"], "closed:player_offline")
        kinds = [e["kind"] for e in self.store.events(limit=10)]
        self.assertIn("player.offline", kinds)

    def test_late_start_is_recorded_not_backdated(self):
        rec = Recorder(self.settings, self.store, pid_fn=lambda: [1])
        t0 = DAY0 + dt.timedelta(minutes=30)
        with fs.connect_ro(self.env.db) as c:
            block = fs.block_at(fs.get_blocks(c, STATION), t0)
            path = fs.get_catalog(c, STATION)[block.content_ids[0]].path
        late = t0 + dt.timedelta(seconds=47)
        self.feed(rec, late + dt.timedelta(seconds=2), timestamp=late.isoformat(), file_path=path,
                  duration="0:00:47/0:07:00")
        row = self.store.conn.execute("SELECT started_at, join_offset_s FROM airings").fetchone()
        self.assertEqual(row["started_at"], "2026-09-26 00:30:47")
        self.assertEqual(row["join_offset_s"], 47.0)
        ev = self.store.events("playout.late_start")
        self.assertEqual(ev[0]["payload"]["skipped_s"], 47.0)

    def test_standby_image_is_an_interruption_not_an_airing(self):
        rec = Recorder(self.settings, self.store, pid_fn=lambda: [1])
        t0 = DAY0
        a0 = self.path("alpha", 0)
        self.feed(rec, t0 + dt.timedelta(seconds=2), timestamp=t0.isoformat(), file_path=a0,
                  duration="0:00:00/0:07:00")
        rec.status_fn = lambda: fs.parse_status({"status": "playing", "network_name": STATION, "channel_number": 10,
                                                 "timestamp": (t0 + dt.timedelta(minutes=3)).isoformat(),
                                                 "title": "Unknown", "file_path": "runtime/standby.png"})
        self.assertEqual(rec.poll(t0 + dt.timedelta(minutes=3, seconds=2))["action"], "interruption")
        self.feed(rec, t0 + dt.timedelta(minutes=4), timestamp=(t0 + dt.timedelta(minutes=4)).isoformat(),
                  file_path=a0, duration="0:04:00/0:07:00")
        rows = self.store.conn.execute("SELECT * FROM airings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ended_at"])
        self.assertEqual(len(self.store.events("playout.interruption")), 1)

    def test_history_metrics(self):
        rec = Recorder(self.settings, self.store, pid_fn=lambda: [1])
        t = DAY0
        for i in range(3):
            p = self.path("bravo", i)
            ts = t + dt.timedelta(minutes=30 * i)
            self.feed(rec, ts + dt.timedelta(minutes=25), timestamp=ts.isoformat(), file_path=p,
                      duration="0:00:00/0:22:00")
        self.feed(rec, t + dt.timedelta(minutes=95), timestamp=(t + dt.timedelta(minutes=90)).isoformat(),
                  file_path=self.path("alpha", 0), duration="0:00:00/0:22:00")
        h = self.cp(DAY0 + dt.timedelta(hours=3)).history(10, "bravo")["programs"][0]
        self.assertEqual(h["airings_7d"], 3)
        self.assertEqual(h["unique_episodes_aired"], 3)
        self.assertEqual(h["unplayed_episodes"], 5)
        self.assertIn("S01E03", h["last_episode_aired"])


class ApiTests(FixtureCase):
    def test_status_now_next_verify(self):
        now = DAY0 + dt.timedelta(hours=10, minutes=5)
        cp = self.cp(now)
        with mock.patch.object(fs, "player_pids", return_value=[]):
            s = cp.status()
        ch = s["channels"][0]
        self.assertEqual(ch["current_block"]["start"], "2026-09-26 10:00:00")
        self.assertEqual(ch["next_block"]["start"], "2026-09-26 10:30:00")
        self.assertEqual(s["active_rotation_count"], 3)
        self.assertTrue(any("not running" in w for w in s["warnings"]))
        self.assertEqual(len(cp.next(10, 2)["next"]), 2)
        v = cp.verify(10)
        self.assertTrue(v["ok"], [c for c in v["checks"] if not c["ok"]])
        cat = cp.catalog(10)
        self.assertEqual(cat["rotation_ready"], 3)

    def test_deactivate_show_removes_it_from_engine_lineup(self):
        now = DAY0 + dt.timedelta(hours=10)
        cp = self.cp(now)
        cp.set_show_active(10, "alpha", False, note="test")
        lineup = cp.plan_lineup(10, DAY0 + dt.timedelta(days=1), DAY0 + dt.timedelta(days=2))["lineup"]
        self.assertNotIn("alpha", {d["tag"] for d in lineup})
        self.assertTrue(any("deactivated by operator" in d["reason"] for d in lineup))
        with self.assertRaises(ValueError):
            cp.set_show_active(10, "nope", True)


import importlib.util as _ilu


@unittest.skipIf(_ilu.find_spec("wdbs.migrate_paths") is None, "wdbs.migrate_paths is withheld from the public export")
class PathMigrationTests(FixtureCase):
    def test_rename_preserves_ids_continuity_and_history(self):
        from wdbs import migrate_paths as mp
        now = DAY0 + dt.timedelta(hours=10, minutes=5)
        preview = {"series": {"Bravo": {"rename": [
            {"existing": f"Season 1/Bravo - S01E{n:02d} - Ep{n}.mkv",
             "new": f"Season 1/Bravo - S01E{n:02d} - Episode {n}.mkv"} for n in range(1, 9)]}}}
        pj = self.env.root / "preview.json"
        pj.write_text(json.dumps(preview))
        with mock.patch.object(mp, "SERIES_TAGS", {"Bravo": "bravo"}):
            orig = mp.build_moves.__defaults__
            mp.build_moves.__defaults__ = ({"Bravo": "bravo"},)
            try:
                dry = mp.run(self.settings, pj, now)
                self.assertTrue(dry["ok"], dry)
                self.assertEqual(dry["moves"], 8)
                with fs.connect_ro(self.env.db) as c:  # dry run left the live DB alone
                    self.assertTrue(any("Ep1.mkv" in e.path for e in fs.get_catalog(c, STATION).values()))
                # simulate Sonarr renaming the files, then apply
                for n in range(1, 9):
                    d = self.env.content_dir / "bravo" / "Season 1"
                    (d / f"Bravo - S01E{n:02d} - Ep{n}.mkv").rename(d / f"Bravo - S01E{n:02d} - Episode {n}.mkv")
                r = mp.run(self.settings, pj, now, apply=True, files_renamed=True, expect=dry["digest"])
            finally:
                mp.build_moves.__defaults__ = orig
        self.assertTrue(r["ok"], r["checks"])
        v = self.cp(now).verify(10)
        self.assertTrue(v["ok"], [c for c in v["checks"] if not c["ok"]])


class PrefetchTests(FixtureCase):
    def test_ranges(self):
        from wdbs.prefetch import HEAD, TAIL, ranges_for
        self.assertEqual(ranges_for(10_000_000, 5, 30), [(0, 10_000_000)])  # small file: whole
        size = 1_000_000_000
        rs = ranges_for(size, 600, 1200)  # half way in
        self.assertEqual(rs[0], (0, HEAD))
        self.assertEqual(rs[-1], (size - TAIL, size))
        self.assertTrue(any(a <= size // 2 < b for a, b in rs))

    def test_targets_and_missing_media(self):
        from wdbs.prefetch import Prefetcher, upcoming_targets
        now = DAY0 + dt.timedelta(hours=9, minutes=55)  # 10:00 and (not yet) 10:30 entries
        with fs.connect_ro(self.env.db) as c:
            blocks = fs.get_blocks(c, STATION)
        self.assertEqual([t.airs_at for t in upcoming_targets(blocks, DAY0 + dt.timedelta(hours=10, minutes=21))],
                         [DAY0 + dt.timedelta(hours=10, minutes=30)])  # the entry already on air is skipped
        ts = upcoming_targets(blocks, now, lookahead=dt.timedelta(minutes=40))
        self.assertEqual([t.airs_at for t in ts], [DAY0 + dt.timedelta(hours=10), DAY0 + dt.timedelta(hours=10, minutes=30)])
        import wdbs.prefetch as pfm
        pfm.LOOKAHEAD, _saved = dt.timedelta(minutes=40), pfm.LOOKAHEAD
        pf = Prefetcher(self.settings, self.store, max_mbps=100, clock=lambda: now)
        os.unlink(ts[1].path)  # upcoming file vanished (e.g. a rename)
        r = pf.run_once()
        self.assertEqual(r["read"], 1)
        self.assertEqual(r["missing"], [ts[1].path])
        self.assertEqual(self.store.events("prefetch.missing_media")[0]["payload"]["path"], ts[1].path)
        self.assertEqual(pf.run_once()["read"], 0)  # already warm; not re-read
        pfm.LOOKAHEAD = _saved
        later = now + dt.timedelta(minutes=31)  # 10:26: the 10:30 entry is 4 min out
        pf.clock = lambda: later
        with open(ts[1].path, "wb") as f:
            f.write(b"x")
        pf.done[ts[1].key] = now  # pretend it was warmed at 10:21
        self.assertEqual(pf.run_once()["read"], 0)  # 10:26 is 4 min before air: outside the 2 min touch window
        pf.clock = lambda: later + dt.timedelta(minutes=2, seconds=30)
        self.assertEqual(pf.run_once()["read"], 1)  # final touch
        self.assertEqual(pf.run_once()["read"], 0)  # only once


class ExtensionPointTests(FixtureCase):
    def test_event_bus_persists_and_dispatches(self):
        from wdbs.events import EventBus
        bus = EventBus(self.store)
        seen = []
        bus.subscribe("airing.*", seen.append)
        eid = bus.publish("airing.started", {"tag": "alpha"}, channel=10)
        bus.publish("replan.applied", {})
        self.assertEqual([e["kind"] for e in seen], ["airing.started"])
        self.assertEqual([e["kind"] for e in bus.since(eid - 1)], ["airing.started", "replan.applied"])

    def test_fs42_channel_is_a_channel(self):
        from wdbs.presentation import Channel, FS42Channel, Registry
        ch = FS42Channel(self.cp(DAY0 + dt.timedelta(hours=1)), 10)
        self.assertIsInstance(ch, Channel)
        reg = Registry()
        reg.register(ch)
        self.assertEqual(reg.list("channel")[0].id, "channel:10")
        self.assertEqual(len(ch.upcoming(2)), 2)


if __name__ == "__main__":
    unittest.main()


class WeatherBreakTests(unittest.TestCase):
    def _plan(self):
        f = lambda s, d: {"path": "/ep.mkv", "skip": s, "duration": d, "is_stream": False, "content_type": "feature"}
        c = lambda n, d: {"path": f"/c/{n}.mp4", "skip": 0, "duration": d, "is_stream": False, "content_type": "commercial"}
        b = {"path": "/bump/id.mp4", "skip": 0, "duration": 4.0, "is_stream": False, "content_type": "bump"}
        return [f(0, 300), b, c("a", 30.5), c("b", 30.7), c("c", 60.1), b, f(300, 300), b, c("d", 15.0), b, f(600, 300)]

    def test_breaks_found_between_features(self):
        from wdbs.weather_break import breaks_of
        self.assertEqual(breaks_of(self._plan()), [(1, 6), (7, 10)])

    def test_best_run_prefers_closest_to_target_and_skips_bumps(self):
        from wdbs.weather_break import _best_run
        plan = self._plan()
        self.assertEqual(_best_run(plan, 1, 6, 60.0), (4, 5))   # the single 60.1s spot
        self.assertEqual(_best_run(plan, 1, 6, 90.0), (3, 5))   # 30.7 + 60.1
        self.assertIsNone(_best_run(plan, 7, 10, 60.0))         # only 15s of commercials

    def test_web_entry_detection(self):
        from wdbs.weather_break import WEB_PREFIX, is_web_entry
        self.assertTrue(is_web_entry({"path": WEB_PREFIX + "http://x/index.html"}))
        self.assertFalse(is_web_entry({"path": "/c/a.mp4"}))

    def _db_with_blocks(self, tmp, start, blocks=4, web_in_block=None):
        import sqlite3
        from pathlib import Path
        db = Path(tmp) / "fs42.db"
        c = sqlite3.connect(str(db))
        c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, liquid_type TEXT, "
                  "start_time TIMESTAMP, end_time TIMESTAMP, break_strategy TEXT, title TEXT, sequence_key TEXT, "
                  "break_info TEXT, content_json TEXT, plan_json TEXT)")
        for i in range(blocks):
            s = start + dt.timedelta(minutes=30 * i)
            plan = self._plan()
            if i == web_in_block:
                plan[4] = {"path": ":autobump:=http://x/", "skip": 0, "duration": 60.1, "is_stream": False,
                           "content_type": "bump"}
            c.execute("INSERT INTO liquid_blocks (station, liquid_type, start_time, end_time, break_strategy, title, "
                      "content_json, plan_json) VALUES ('S', 'LiquidBlock', ?, ?, 'standard', ?, '1', ?)",
                      (s.strftime("%Y-%m-%d %H:%M:%S"), (s + dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                       f"ep{i}", json.dumps(plan)))
        c.commit()
        c.close()
        return db

    def test_hourly_one_per_hour_near_eight_and_idempotent(self):
        import tempfile
        from pathlib import Path
        from wdbs.weather_break import compute_hourly
        st = fs.Station(10, "S", "standard", Path("x.json"), {})
        start = dt.datetime(2026, 9, 27, 6, 0)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start, blocks=4)
            with fs.connect_ro(db) as conn:
                plans, skipped = compute_hourly(conn, st, now=start - dt.timedelta(hours=1), url="http://w/")
            self.assertEqual([p.block_start[11:16] for p in plans], ["06:00", "07:00"])
            self.assertEqual([p.segment_s for p in plans], [60.1, 60.1])   # :05:04 break, single 60.1s spot
            self.assertEqual(skipped, [])
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start, blocks=4, web_in_block=1)   # 06:30 already has one
            with fs.connect_ro(db) as conn:
                plans, _ = compute_hourly(conn, st, now=start - dt.timedelta(hours=1), url="http://w/")
            self.assertEqual([p.block_start[11:16] for p in plans], ["07:00"])

    def test_hourly_respects_lead_time(self):
        import tempfile
        from pathlib import Path
        from wdbs.weather_break import compute_hourly
        st = fs.Station(10, "S", "standard", Path("x.json"), {})
        start = dt.datetime(2026, 9, 27, 6, 0)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start, blocks=4)
            with fs.connect_ro(db) as conn:   # 06:00 block starts in 5 min -> use 06:30 block for hour 6
                plans, _ = compute_hourly(conn, st, now=start - dt.timedelta(minutes=5), url="http://w/")
            self.assertEqual([p.block_start[11:16] for p in plans], ["06:30", "07:00"])

    def test_intro_leads_segment_and_keeps_block_length(self):
        import tempfile
        from pathlib import Path
        from wdbs.weather_break import Intro, compute_hourly
        st = fs.Station(10, "S", "standard", Path("x.json"), {})
        start = dt.datetime(2026, 9, 27, 6, 0)
        intro = Intro("/assets/weather_intro.mp4", 4.0)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start, blocks=2)
            with fs.connect_ro(db) as conn:
                plans, _ = compute_hourly(conn, st, now=start - dt.timedelta(hours=1), url="http://w/", intro=intro)
            p = plans[0]
            new, old = json.loads(p.new_plan_json), json.loads(p.old_plan_json)
            self.assertEqual(p.inserted, 2)
            self.assertEqual(new[p.first_index]["path"], intro.path)
            self.assertAlmostEqual(new[p.first_index + 1]["duration"], 60.1 - 4.0)
            self.assertAlmostEqual(sum(e["duration"] for e in new), sum(e["duration"] for e in old))

    def test_upgrade_adds_intro_once(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from wdbs.weather_break import Intro, compute_intro_upgrade
        st = fs.Station(10, "S", "standard", Path("x.json"), {})
        start = dt.datetime(2026, 9, 27, 6, 0)
        intro = Intro("/assets/weather_intro.mp4", 4.0)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start, blocks=3, web_in_block=1)
            with fs.connect_ro(db) as conn:
                plans = compute_intro_upgrade(conn, st, now=start - dt.timedelta(hours=1), intro=intro)
            self.assertEqual([p.block_start[11:16] for p in plans], ["06:30"])
            new = json.loads(plans[0].new_plan_json)
            i = plans[0].first_index
            self.assertEqual((new[i]["path"], new[i + 1]["duration"]), (intro.path, 60.1 - 4.0))
            self.assertEqual({new[i]["content_type"], new[i + 1]["content_type"]}, {"weather"})
            c = sqlite3.connect(str(db))
            c.execute("UPDATE liquid_blocks SET plan_json = ? WHERE id = ?", (plans[0].new_plan_json, plans[0].block_id))
            c.commit()
            c.close()
            with fs.connect_ro(db) as conn:
                self.assertEqual(compute_intro_upgrade(conn, st, now=start - dt.timedelta(hours=1), intro=intro), [])

    def test_overnight_state_fails_closed(self):
        from wdbs import weather_break as wb
        cases = [("inactive\n", "clear"), ("failed\n", "clear"), ("active\n", "running"), ("activating\n", "running")]
        for out, want in cases:
            with mock.patch("subprocess.run", return_value=mock.Mock(stdout=out, stderr="", returncode=0)):
                self.assertEqual(wb.overnight_state(), want)
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="", stderr="Failed to connect to bus", returncode=1)):
            self.assertTrue(wb.overnight_state().startswith("unknown"))
        with mock.patch("subprocess.run", side_effect=OSError("no systemctl")):
            self.assertTrue(wb.overnight_state().startswith("unknown"))

    def test_prune_keeps_recent_and_newest_five(self):
        import tempfile, time
        from pathlib import Path
        from wdbs import weather_break as wb
        from wdbs.config import Settings
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(backup_dir=Path(tmp), fs42_db_override=Path(tmp) / "fs42_fluid.db")
            now = time.time()
            for i in range(8):   # 8 backups, one per day going back
                for ext in (".bak", ".json"):
                    f = Path(tmp) / f"fs42_fluid.db.wdbs-weather-2026090{i}-000000{ext}"
                    f.write_text("x")
                    os.utime(f, (now - i * 86400 - 60, now - i * 86400 - 60))
            other = Path(tmp) / "fs42_fluid.db.wdbs-replan-20260901-000000.bak"
            other.write_text("x")
            os.utime(other, (now - 30 * 86400, now - 30 * 86400))
            removed = wb.prune_backups(s)
            left = sorted(p.name for p in Path(tmp).glob("*.wdbs-weather-*.bak"))
            self.assertEqual(len(left), 5)                 # newest five kept (days 0-4)
            self.assertEqual(len(removed), 6)              # days 5-7: .bak + .json each
            self.assertTrue(other.exists())                # other tools' backups untouched

    def test_refused_write_does_not_restore(self):
        import tempfile
        from pathlib import Path
        from wdbs import weather_break as wb
        from wdbs.config import Settings
        st = fs.Station(10, "S", "standard", Path("x.json"), {})
        start = dt.datetime.now().replace(microsecond=0) + dt.timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start.replace(minute=0, second=0), blocks=2)
            s = Settings(state_db=Path(tmp) / "wdbs.db", backup_dir=Path(tmp), fs42_db_override=db)
            with fs.connect_ro(db) as conn:
                plans, _ = wb.compute_hourly(conn, st, now=dt.datetime.now(), url="http://w/")
            plans[0].old_plan_json = "[]"                  # simulate the block changing after the plan
            with mock.patch.object(wb, "overnight_state", return_value="clear"), \
                 mock.patch.object(wb.rp, "sqlite_restore") as restore:
                r = wb.apply(s, plans, dt.datetime.now())
            self.assertFalse(r["ok"])
            self.assertFalse(r["restored"])
            restore.assert_not_called()

    def test_upgrade_refreshes_url(self):
        import tempfile
        from pathlib import Path
        from wdbs.weather_break import WEB_PREFIX, Intro, compute_intro_upgrade
        st = fs.Station(10, "S", "standard", Path("x.json"), {})
        start = dt.datetime(2026, 9, 27, 6, 0)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start, blocks=2, web_in_block=0)
            with fs.connect_ro(db) as conn:
                plans = compute_intro_upgrade(conn, st, now=start - dt.timedelta(hours=1),
                                              intro=Intro("/a/i.mp4", 4.0), url="http://new/")
            new = json.loads(plans[0].new_plan_json)
            self.assertEqual(new[plans[0].first_index + 1]["path"], WEB_PREFIX + "http://new/")

    def test_asap_skips_blocks_with_weather_and_uses_morning_intro(self):
        import tempfile
        from pathlib import Path
        from wdbs.weather_break import Intro, compute_asap
        st = fs.Station(10, "S", "standard", Path("x.json"), {})
        start = dt.datetime(2026, 9, 27, 6, 0)
        regular, morning = Intro("/a/regular.mp4", 4.0), Intro("/a/morning.mp4", 5.0)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db_with_blocks(tmp, start, blocks=3, web_in_block=0)   # 06:00 already has weather
            with fs.connect_ro(db) as conn:
                p = compute_asap(conn, st, now=start - dt.timedelta(hours=1), url="http://w/",
                                 intro=regular, morning_intro=morning)
            self.assertEqual(p.block_start[11:16], "06:30")
            self.assertEqual(p.intro, morning.path)                      # airs before 11:00

    def test_pick_intro_by_hour(self):
        from wdbs.weather_break import Intro, pick_intro
        regular, morning = Intro("/r.mp4", 4.0), Intro("/m.mp4", 5.0)
        self.assertIs(pick_intro(regular, morning, dt.datetime(2026, 9, 27, 4, 59)), regular)
        self.assertIs(pick_intro(regular, morning, dt.datetime(2026, 9, 27, 5, 0)), morning)
        self.assertIs(pick_intro(regular, morning, dt.datetime(2026, 9, 27, 10, 59)), morning)
        self.assertIs(pick_intro(regular, morning, dt.datetime(2026, 9, 27, 11, 0)), regular)
        self.assertIs(pick_intro(regular, None, dt.datetime(2026, 9, 27, 7, 0)), regular)
