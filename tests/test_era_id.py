import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wdbs import era_id as eid
from wdbs import fs42_adapter as fs
from wdbs import weather_break as wb
from wdbs.weather_break import Intro

ASSETS = "/path/to/FieldStation42/wdbs_assets/era"
IDS = {e: Intro(f"{ASSETS}/id_{e}.mp4", 4.0) for e in ("70s", "80s", "90s", "00s")}
ERAS = {"sanford_and_son": "70s", "seinfeld": "90s"}
BUMP = "/path/to/FieldStation42/catalog/example_sitcoms/bump/EXAMPLE_SITCOMS_ID.mp4"
START = dt.datetime(2026, 9, 29, 6, 0)
ST = fs.Station(10, "S", "standard", Path("x.json"), {})
BUMPS = [1, 4, 6, 8]          # station-ID positions in plan()
GENERIC = Intro(BUMP, 4.0)


def plan(bump_len=4.0, weather=False):
    f = lambda s: {"path": "/ep.mkv", "skip": s, "duration": 300.0, "is_stream": False,
                   "content_type": "feature", "media_type": "video"}
    b = lambda: {"path": BUMP, "skip": 0, "duration": bump_len, "is_stream": False,
                 "content_type": "bump", "media_type": "video"}
    c = lambda n, d: {"path": f"/c/{n}.mp4", "skip": 0, "duration": d, "is_stream": False,
                      "content_type": "commercial", "media_type": "video"}
    brk = [c("a", 30.0), c("b", 30.0)]
    if weather:
        brk = [{"path": "/path/to/FieldStation42/wdbs_assets/weather_intro.mp4", "skip": 0, "duration": 4.0,
                "is_stream": False, "content_type": "weather", "media_type": "video"},
               {"path": ":autobump:=http://w/", "skip": 0, "duration": 56.0, "is_stream": False,
                "content_type": "weather", "media_type": "video"}]
    return [f(0), b(), *brk, b(), f(300), b(), c("d", 15.0), b(), f(600), c("e", 10.0)]


def make_db(tmp, tags, start=START, overrides=None):
    db = Path(tmp) / "fs42.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE liquid_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT, liquid_type TEXT, "
              "start_time TIMESTAMP, end_time TIMESTAMP, break_strategy TEXT, title TEXT, sequence_key TEXT, "
              "break_info TEXT, content_json TEXT, plan_json TEXT)")
    for i, tag in enumerate(tags):
        s = start + dt.timedelta(minutes=30 * i)
        c.execute("INSERT INTO liquid_blocks (station, liquid_type, start_time, end_time, break_strategy, title, "
                  "sequence_key, content_json, plan_json) VALUES ('S', 'LiquidBlock', ?, ?, 'standard', ?, ?, '1', ?)",
                  (s.strftime("%Y-%m-%d %H:%M:%S"), (s + dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                   f"{tag} ep{i}", json.dumps({"sequence_name": tag, "tag_path": tag}),
                   json.dumps(plan(**(overrides or {}).get(i, {})))))
    c.commit()
    c.close()
    return db


def write_plans(db, plans):
    c = sqlite3.connect(str(db))
    for p in plans:
        c.execute("UPDATE liquid_blocks SET plan_json = ? WHERE id = ?", (p.new_plan_json, p.block_id))
    c.commit()
    c.close()


def compute(db, now=START - dt.timedelta(hours=1), eras=ERAS, at=None):
    with fs.connect_ro(db) as conn:
        return eid.compute(conn, ST, now=now, eras=eras, ids=IDS, assets_dir=ASSETS, at=at)


class EraIdPlanTests(unittest.TestCase):
    def test_swaps_every_station_id_in_rotation_blocks_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld", "arrested_s1"])
            plans, skipped = compute(db)
            self.assertEqual([(p.tag, p.era, p.swapped) for p in plans],
                             [("sanford_and_son", "70s", BUMPS), ("seinfeld", "90s", BUMPS)])
            self.assertEqual(skipped, [])
            p = plans[0]
            old, new = json.loads(p.old_plan_json), json.loads(p.new_plan_json)
            for i, (o, n) in enumerate(zip(old, new)):
                if i in BUMPS:
                    self.assertEqual(n, {**o, "path": IDS["70s"].path})
                else:
                    self.assertEqual(n, o)
            self.assertEqual(sum(e["duration"] for e in new), sum(e["duration"] for e in old))

    def test_idempotent_and_regenerated_blocks_swap_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld"])
            write_plans(db, compute(db)[0])
            self.assertEqual(compute(db), ([], []))                     # already right: nothing to do
            c = sqlite3.connect(str(db))                                # a replan regenerates block 1
            c.execute("UPDATE liquid_blocks SET plan_json = ? WHERE id = 1", (json.dumps(plan()),))
            c.commit()
            c.close()
            self.assertEqual([p.block_id for p in compute(db)[0]], [1])

    def test_reswaps_when_the_era_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld"])
            write_plans(db, compute(db)[0])
            plans, _ = compute(db, eras={"sanford_and_son": "70s", "seinfeld": "80s"})
            self.assertEqual([(p.tag, p.era, p.swapped) for p in plans], [("seinfeld", "80s", BUMPS)])
            self.assertEqual({json.loads(plans[0].new_plan_json)[i]["path"] for i in BUMPS}, {IDS["80s"].path})

    def test_respects_lead_time_and_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld", "sanford_and_son"])
            plans, _ = compute(db, now=START - dt.timedelta(minutes=5))   # 06:00 starts too soon
            self.assertEqual([p.block_start[11:16] for p in plans], ["06:30", "07:00"])
            plans, _ = compute(db, at=START + dt.timedelta(minutes=45))
            self.assertEqual([p.block_start[11:16] for p in plans], ["06:30"])

    def test_wrong_length_slot_leaves_block_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld"], overrides={0: {"bump_len": 5.0}})
            plans, skipped = compute(db)
            self.assertEqual([p.block_id for p in plans], [2])
            self.assertEqual([s["block_id"] for s in skipped], [1])
            self.assertIn("4.000", skipped[0]["reason"])

    def test_weather_entries_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son"], overrides={0: {"weather": True}})
            p = compute(db)[0][0]
            old, new = json.loads(p.old_plan_json), json.loads(p.new_plan_json)
            self.assertEqual([e for e in new if e["content_type"] == "weather"],
                             [e for e in old if e["content_type"] == "weather"])

    def test_is_station_id(self):
        self.assertTrue(eid.is_station_id({"content_type": "bump", "path": BUMP}, ASSETS))
        self.assertTrue(eid.is_station_id({"content_type": "bump", "path": f"{ASSETS}/id_70s.mp4"}, ASSETS))
        self.assertFalse(eid.is_station_id({"content_type": "weather", "path": f"{ASSETS}/id_70s.mp4"}, ASSETS))
        self.assertFalse(eid.is_station_id({"content_type": "commercial", "path": "/c/a.mp4"}, ASSETS))
        self.assertFalse(eid.is_station_id({"content_type": "bump", "path": ":autobump:=http://w/"}, ASSETS))

    def test_load_ids_refuses_if_any_era_is_missing(self):
        def fake(path):
            if "id_80s" in path:
                raise wb.WeatherBreakRefused(f"Intro file not found: {path}")
            return Intro(path, 4.0)
        with mock.patch.object(wb, "load_intro", side_effect=fake):
            with self.assertRaises(eid.EraIdRefused):
                eid.load_ids(ASSETS)
        with mock.patch.object(wb, "load_intro", side_effect=lambda p: Intro(p, 4.0)):
            self.assertEqual(sorted(eid.load_ids(ASSETS)), ["00s", "70s", "80s", "90s"])
            self.assertEqual(eid.load_ids(ASSETS)["70s"].path, f"{ASSETS}/id_70s.mp4")

    def test_off_puts_the_generic_id_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld", "arrested_s1"])
            era_plans = compute(db)[0]
            write_plans(db, era_plans)
            with fs.connect_ro(db) as conn:
                plans, skipped = eid.compute_off(conn, ST, now=START - dt.timedelta(hours=1),
                                                  generic=GENERIC, assets_dir=ASSETS)
            self.assertEqual([p.block_id for p in plans], [1, 2])
            self.assertEqual([p.swapped for p in plans], [BUMPS, BUMPS])
            self.assertEqual(skipped, [])
            era_new_by_id = {p.block_id: json.loads(p.new_plan_json) for p in era_plans}
            for p in plans:
                new = json.loads(p.new_plan_json)
                self.assertEqual({new[i]["path"] for i in BUMPS}, {GENERIC.path})
                era_new = era_new_by_id[p.block_id]
                for i, (o, n) in enumerate(zip(era_new, new)):
                    if i not in BUMPS:
                        self.assertEqual(o, n)
            write_plans(db, plans)
            with fs.connect_ro(db) as conn:
                self.assertEqual(eid.compute_off(conn, ST, now=START - dt.timedelta(hours=1),
                                                  generic=GENERIC, assets_dir=ASSETS), ([], []))

    def test_off_works_after_another_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld"])
            write_plans(db, compute(db)[0])
            c = sqlite3.connect(str(db))                                 # simulate a weather write on block 1
            p = json.loads(c.execute("SELECT plan_json FROM liquid_blocks WHERE id = 1").fetchone()[0])
            p[2] = {**p[2], "path": "/c/changed.mp4"}
            c.execute("UPDATE liquid_blocks SET plan_json = ? WHERE id = 1", (json.dumps(p),))
            c.commit()
            c.close()
            with fs.connect_ro(db) as conn:
                plans, skipped = eid.compute_off(conn, ST, now=START - dt.timedelta(hours=1),
                                                  generic=GENERIC, assets_dir=ASSETS)
            self.assertIn(1, [p.block_id for p in plans])
            by_id = {p.block_id: p for p in plans}
            self.assertEqual({json.loads(by_id[1].new_plan_json)[i]["path"] for i in BUMPS}, {GENERIC.path})

    def test_off_leaves_a_wrong_length_block_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp, ["sanford_and_son", "seinfeld"])
            write_plans(db, compute(db)[0])
            with fs.connect_ro(db) as conn:
                plans, skipped = eid.compute_off(conn, ST, now=START - dt.timedelta(hours=1),
                                                  generic=Intro(BUMP, 5.0), assets_dir=ASSETS)
            self.assertEqual(plans, [])
            self.assertEqual([s["block_id"] for s in skipped], [1, 2])


def fake_load_check(settings, db, ids):
    c = sqlite3.connect(str(db))
    out = {}
    for bid in ids:
        p = json.loads(c.execute("SELECT plan_json FROM liquid_blocks WHERE id = ?", (bid,)).fetchone()[0])
        out[bid] = {"entries": len(p), "web": sum(1 for e in p if wb.is_web_entry(e)),
                    "total": sum(e["duration"] for e in p)}
    c.close()
    return {"ok": True, "blocks": out}


class EraIdWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # at least an hour out whatever the time of day (blocks must start >= MIN_LEAD from now)
        self.start = (dt.datetime.now() + dt.timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        self.db = make_db(self.tmp.name, ["sanford_and_son", "seinfeld"], start=self.start)
        from wdbs.config import Settings
        self.s = Settings(fs42_root=Path(self.tmp.name), state_db=Path(self.tmp.name) / "wdbs.db",
                          backup_dir=Path(self.tmp.name), fs42_db_override=self.db)
        with fs.connect_ro(self.db) as conn:
            self.plans, _ = eid.compute(conn, ST, now=dt.datetime.now(), eras=ERAS, ids=IDS, assets_dir=ASSETS)
        self.patches = [mock.patch.object(wb, "overnight_state", return_value="clear"),
                        mock.patch.object(wb, "_fs42_load_check", side_effect=fake_load_check)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def plans_in_db(self):
        c = sqlite3.connect(str(self.db))
        rows = dict(c.execute("SELECT id, plan_json FROM liquid_blocks"))
        c.close()
        return rows

    def test_rehearse_leaves_the_live_db_alone(self):
        before = self.plans_in_db()
        with mock.patch.object(wb, "_measure", return_value=4.0):
            r = eid.rehearse(self.s, self.plans)
        self.assertTrue(r["ok"], r["checks"])
        self.assertEqual(self.plans_in_db(), before)

    def test_apply_then_revert(self):
        before = self.plans_in_db()
        with mock.patch.object(wb, "_measure", return_value=4.0):
            r = eid.apply(self.s, self.plans, dt.datetime.now())
        self.assertTrue(r["ok"], r["checks"])
        self.assertFalse(r["restored"])
        self.assertIn("wdbs-eraid-", r["backup"])
        after = self.plans_in_db()
        self.assertEqual({json.loads(after[1])[i]["path"] for i in BUMPS}, {IDS["70s"].path})
        self.assertEqual({json.loads(after[2])[i]["path"] for i in BUMPS}, {IDS["90s"].path})
        rv = wb.revert(self.s, Path(r["sidecar"]), dt.datetime.now())
        self.assertTrue(rv["ok"], rv)
        self.assertEqual(self.plans_in_db(), before)

    def test_verify_catches_a_wrong_length_id_and_restores(self):
        before = self.plans_in_db()
        with mock.patch.object(wb, "_measure", return_value=4.5):     # the deployed ID is 4.5 s
            r = eid.apply(self.s, self.plans, dt.datetime.now())
        self.assertFalse(r["ok"])
        self.assertTrue(r["restored"])
        self.assertEqual(self.plans_in_db(), before)
        self.assertIn("era ID files present, lengths match the schedule",
                      [c["check"] for c in r["checks"] if not c["ok"]])

    def test_apply_refuses_a_block_that_starts_too_soon(self):
        with self.assertRaises(eid.EraIdRefused):
            eid.apply(self.s, self.plans, dt.datetime.fromisoformat(self.plans[0].block_start) - dt.timedelta(minutes=5))

    def test_off_switch_apply_restores_generic_id(self):
        with mock.patch.object(wb, "_measure", return_value=4.0):
            r = eid.apply(self.s, self.plans, dt.datetime.now())
        self.assertTrue(r["ok"], r["checks"])
        with fs.connect_ro(self.db) as conn:
            off_plans, skipped = eid.compute_off(conn, ST, now=dt.datetime.now(), generic=GENERIC, assets_dir=ASSETS)
        self.assertEqual(skipped, [])
        with mock.patch.object(wb, "_measure", return_value=4.0):
            r2 = eid.apply(self.s, off_plans, dt.datetime.now())
        self.assertTrue(r2["ok"], r2["checks"])
        after = self.plans_in_db()
        self.assertEqual({json.loads(after[1])[i]["path"] for i in BUMPS}, {GENERIC.path})
        self.assertEqual({json.loads(after[2])[i]["path"] for i in BUMPS}, {GENERIC.path})

    def test_prune_by_label(self):
        import os
        import time as _t
        now = _t.time()
        for i in range(8):
            for label in ("weather", "eraid"):
                f = Path(self.tmp.name) / f"fs42.db.wdbs-{label}-2026090{i}-000000.bak"
                f.write_text("x")
                os.utime(f, (now - i * 86400 - 60, now - i * 86400 - 60))
        removed = wb.prune_backups(self.s, label="eraid")
        self.assertEqual(len([n for n in removed if "eraid" in n]), 3)
        self.assertFalse([n for n in removed if "weather" in n])


class EraIdApiTests(unittest.TestCase):
    def setUp(self):
        from wdbs.config import Settings
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "fs42"
        (root / "confs").mkdir(parents=True)
        (root / "confs" / "test.json").write_text(json.dumps({"station_conf": {
            "network_name": "S", "channel_number": 10, "network_type": "standard"}}))
        self.db = make_db(self.tmp.name, ["sanford_and_son", "seinfeld"], start=START)
        self.manifest = Path(self.tmp.name) / "manifest.json"
        self.manifest.write_text(json.dumps({"schema": 1, "channel": 10, "generated_at": "2026-09-28T00:00:00+00:00",
                                             "programs": [{"tag": "sanford_and_son", "premiere_year": 1972},
                                                          {"tag": "seinfeld", "premiere_year": 1989,
                                                           "era_override": "90s"}]}))
        self.s = Settings(fs42_root=root, state_db=Path(self.tmp.name) / "wdbs.db", manifest=self.manifest,
                          backup_dir=Path(self.tmp.name), fs42_db_override=self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def cp(self):
        from wdbs.api import ControlPlane
        return ControlPlane(self.s, clock=lambda: START - dt.timedelta(hours=1))

    def test_dry_run_plans_every_rotation_block_and_writes_nothing(self):
        with mock.patch.object(eid, "load_ids", return_value=IDS), \
             mock.patch.object(wb, "_fs42_load_check", side_effect=fake_load_check), \
             mock.patch.object(wb, "_measure", return_value=4.0):
            r = self.cp().era_ids(10)
        self.assertEqual(r["mode"], "dry-run")
        self.assertEqual([(p["tag"], p["era"]) for p in r["plans"]], [("sanford_and_son", "70s"), ("seinfeld", "90s")])
        self.assertTrue(r["result"]["ok"], r["result"])
        with fs.connect_ro(self.db) as conn:
            self.assertTrue(all(BUMP in b.plan_json for b in fs.get_blocks(conn, "S")))

    def test_apply_needs_the_reviewed_digest(self):
        with mock.patch.object(eid, "load_ids", return_value=IDS), \
             mock.patch.object(wb, "_fs42_load_check", side_effect=fake_load_check), \
             mock.patch.object(wb, "_measure", return_value=4.0):
            with self.assertRaises(eid.EraIdRefused):
                self.cp().era_ids(10, apply=True, expect_digest="0000000000000000")

    def test_unreadable_manifest_refuses(self):
        self.manifest.write_text("{broken")
        with mock.patch.object(eid, "load_ids", return_value=IDS):
            with self.assertRaises(eid.EraIdRefused):
                self.cp().era_ids(10)

    def test_off_needs_one_catalog_station_id_and_skips_the_manifest(self):
        with fs.connect_ro(self.db) as conn:
            plans, _ = eid.compute(conn, ST, now=START - dt.timedelta(hours=1), eras=ERAS, ids=IDS, assets_dir=ASSETS)
        write_plans(self.db, plans)
        self.manifest.write_text("{broken")                    # --off must not need the manifest
        c = sqlite3.connect(str(self.db))
        c.execute("CREATE TABLE catalog_entries (id INTEGER PRIMARY KEY, station TEXT, path TEXT, realpath TEXT, "
                  "title TEXT, duration REAL, tag TEXT, count INTEGER, content_type TEXT)")
        c.commit()
        with mock.patch.object(wb, "load_intro", side_effect=lambda p: Intro(p, 4.0)), \
             mock.patch.object(wb, "_fs42_load_check", side_effect=fake_load_check), \
             mock.patch.object(wb, "_measure", return_value=4.0):
            with self.assertRaises(eid.EraIdRefused):             # no station ID in the catalog yet
                self.cp().era_ids(10, off=True, assets_dir=ASSETS)
            c.execute("INSERT INTO catalog_entries (station, path, title, duration, tag, count, content_type) "
                      "VALUES ('S', ?, 'ID', 4.0, 'bump', 0, 'bump')", (BUMP,))
            c.commit()
            c.close()
            r = self.cp().era_ids(10, off=True, assets_dir=ASSETS)
        self.assertTrue(r["off"])
        self.assertEqual([(p["era"], p["swapped"]) for p in r["plans"]], [("generic", BUMPS), ("generic", BUMPS)])
        self.assertTrue(r["result"]["ok"], r["result"])

    def test_auto_with_a_failed_rehearsal_leaves_the_db_alone(self):
        with mock.patch.object(eid, "load_ids", return_value=IDS), \
             mock.patch.object(wb, "_fs42_load_check", side_effect=fake_load_check), \
             mock.patch.object(wb, "overnight_state", return_value="clear"), \
             mock.patch.object(wb, "_measure", return_value=4.5):          # deployed IDs measure wrong
            r = self.cp().era_ids(10, auto=True)
        self.assertFalse(r["result"]["ok"])
        self.assertIn("rehearsal failed", r["result"]["note"])
        with fs.connect_ro(self.db) as conn:
            self.assertTrue(all(BUMP in b.plan_json for b in fs.get_blocks(conn, "S")))

    def test_cli_refuses_apply_without_expect(self):
        from wdbs.cli import build_parser, cmd_era_id
        a = build_parser().parse_args(["era-id", "--apply"])
        self.assertEqual(cmd_era_id(mock.Mock(), a), 2)
        a = build_parser().parse_args(["era-id", "--at", "today 21:00", "--auto", "--no-reload"])
        self.assertEqual((a.channel, a.at, a.auto, a.no_reload), (10, "today 21:00", True, True))
        a = build_parser().parse_args(["era-id", "--off"])
        self.assertTrue(a.off)


if __name__ == "__main__":
    unittest.main()
