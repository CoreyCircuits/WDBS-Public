"""today/prodesk/wdbs_today_feed.py: the Money source (the optional Ledger service -> home.json)."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "today" / "prodesk" / "wdbs_today_feed.py"
_spec = importlib.util.spec_from_file_location("wdbs_today_feed", FEED)
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

GOOD = {"available": 250.00, "spent_today": 12.50, "spent_month": 480.25, "upcoming_7d": 90.0,
        "status": "clear", "as_of": "2026-10-07T17:00:00.000Z", "currency": "USD",
        "bank_state": "manual", "bank_synced_at": None, "bank_data_age_hours": None}
TOKEN = "ssl_wdbs_unit_test_token_000000000000000000"


class ParseMoney(unittest.TestCase):
    def test_copies_only_the_shown_fields(self):
        m = feed.parse_money(GOOD)
        self.assertEqual(set(m), {"available", "spent_today", "upcoming_7d", "status", "as_of",
                                  "bank_state", "bank_synced_at", "bank_data_age_hours"})
        self.assertEqual(m["available"], 250.00)
        self.assertEqual(m["as_of"], "2026-10-07T17:00:00+00:00")

    def test_extra_fields_never_pass_through(self):
        m = feed.parse_money({**GOOD, "transactions": [{"payee": "x"}], "token": "t"})
        self.assertNotIn("transactions", m)
        self.assertNotIn("token", m)

    def test_rejects_bad_values(self):
        for bad in ({"available": "428"}, {"available": True}, {"available": float("nan")}, {"spent_today": -1},
                    {"upcoming_7d": -0.5}, {"status": "great"}, {"as_of": "yesterday"}, {"available": None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                feed.parse_money({**GOOD, **bad})
        with self.assertRaises(ValueError):
            feed.parse_money([GOOD])

    def test_bank_freshness_passes_through_validated(self):
        m = feed.parse_money({**GOOD, "bank_state": "stale", "bank_synced_at": "2026-10-06T22:00:00Z", "bank_data_age_hours": 31.04})
        self.assertEqual((m["bank_state"], m["bank_synced_at"], m["bank_data_age_hours"]), ("stale", "2026-10-06T22:00:00+00:00", 31.0))

    def test_missing_bank_fields_fail_closed_as_unknown(self):
        legacy = {k: v for k, v in GOOD.items() if not k.startswith("bank_")}
        m = feed.parse_money(legacy)
        self.assertEqual((m["bank_state"], m["bank_synced_at"], m["bank_data_age_hours"]), ("unknown", None, None))

    def test_rejects_bad_bank_fields(self):
        for bad in ({"bank_state": "fine"}, {"bank_state": None}, {"bank_synced_at": "yesterday"}, {"bank_data_age_hours": -1},
                    {"bank_data_age_hours": "5"}, {"bank_data_age_hours": True}, {"bank_data_age_hours": float("nan")}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                feed.parse_money({**GOOD, **bad})

    def test_error_text_is_class_and_status_only(self):
        e = urllib.error.HTTPError("http://127.0.0.1:8760/v1/summary", 401, "Unauthorized", {}, None)
        self.assertEqual(feed.ledger_error(e), "HTTPError HTTP 401")
        self.assertEqual(feed.ledger_error(ValueError("ledger: bad status " + TOKEN)), "ValueError")


class _Ledger(BaseHTTPRequestHandler):
    status, body, seen = 200, GOOD, []

    def do_GET(self):
        _Ledger.seen.append((self.path, self.headers.get("Authorization")))
        data = json.dumps(_Ledger.body).encode()
        self.send_response(_Ledger.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class FeedRun(unittest.TestCase):
    """Runs the real feed script end to end against a fake Ledger, offline otherwise."""

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Ledger)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    # The real main(), with calendar/Notion/NWS read from the saved fixtures (no internet) but the
    # Ledger call going over real HTTP to the fake Ledger above.
    BOOT = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('f', sys.argv[1]); f = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(f)\n"
        "fx = f.FixtureSources(sys.argv[2])\n"
        "f.LiveSources.ics = lambda s, u: fx.ics('calendar-1.ics')\n"
        "f.LiveSources.notion_query = lambda s, *a: fx.notion_query(*a)\n"
        "f.LiveSources.nws = lambda s, u: fx.nws(u)\n"
        "sys.argv = ['feed'] + sys.argv[3:]\n"
        "sys.exit(f.main())\n"
    )

    def run_feed(self, out: Path, env_extra: dict):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_", "NOTION_", "WDBS_TODAY_"))}
        env.update({"NOTION_TOKEN": "fixture", "WDBS_TODAY_ICS_URLS": "fixture://calendar-1.ics"}, **env_extra)
        cfg = ROOT / "today" / "prodesk" / "today-config.json"
        fixtures = ROOT / "today" / "tests" / "fixtures"
        return subprocess.run([sys.executable, "-c", self.BOOT, str(FEED), str(fixtures), "--config", str(cfg),
                               "--out", str(out), "--state", str(out.parent / "state.json")],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_live_path_sends_token_and_writes_only_summary(self):
        _Ledger.status, _Ledger.body, _Ledger.seen = 200, GOOD, []
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "home.json"
            r = self.run_feed(out, {"LEDGER_URL": self.url, "LEDGER_TOKEN": TOKEN})
            text = out.read_text()
            home = json.loads(text)
        self.assertEqual(_Ledger.seen, [("/v1/summary", f"Bearer {TOKEN}")])
        self.assertEqual(home["money"]["available"], 250.00)
        self.assertNotIn("spent_month", home["money"])
        self.assertTrue(home["health"]["money"]["ok"])
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN, r.stderr + r.stdout)
        self.assertIn("money=ok", r.stderr)

    def test_ledger_down_keeps_last_good_and_flags_health(self):
        _Ledger.status, _Ledger.body = 200, GOOD
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "home.json"
            self.run_feed(out, {"LEDGER_URL": self.url, "LEDGER_TOKEN": TOKEN})
            r = self.run_feed(out, {"LEDGER_URL": "http://127.0.0.1:9", "LEDGER_TOKEN": TOKEN})  # nothing listens
            home = json.loads(out.read_text())
        self.assertEqual(home["money"]["available"], 250.00, "last good summary kept")
        self.assertFalse(home["health"]["money"]["ok"])
        self.assertEqual(home["health"]["money"]["error"], "URLError")
        self.assertIn("money=stale", r.stderr)
        self.assertEqual(r.returncode, 0, "a Ledger outage never fails the feed")

    def test_unauthorized_is_reported_without_detail(self):
        _Ledger.status, _Ledger.body = 401, {"error": "unauthorized"}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "home.json"
            self.run_feed(out, {"LEDGER_URL": self.url, "LEDGER_TOKEN": TOKEN})
            home = json.loads(out.read_text())
        _Ledger.status, _Ledger.body = 200, GOOD
        self.assertIsNone(home["money"])
        self.assertEqual(home["health"]["money"]["error"], "HTTPError HTTP 401")

    def test_off_when_not_configured(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "home.json"
            out.write_text(json.dumps({"money": feed.parse_money(GOOD)}))  # stale copy from when it was on
            r = self.run_feed(out, {})
            home = json.loads(out.read_text())
        self.assertIsNone(home["money"])
        self.assertNotIn("money", home["health"])
        self.assertIn("money=off", r.stderr)


if __name__ == "__main__":
    unittest.main()
