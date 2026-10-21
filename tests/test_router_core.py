"""WDBS Channel Router pure logic (fs42_overlay/wdbs_router_core.py)."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("wdbs_router_core", ROOT / "fs42_overlay" / "wdbs_router_core.py")
core = importlib.util.module_from_spec(_spec)
sys.modules["wdbs_router_core"] = core
_spec.loader.exec_module(core)

CH9 = json.loads((ROOT / "config/stations/wdbs_stream.json").read_text())["station_conf"]
CH10 = {"network_name": "EXAMPLE SITCOMS", "channel_number": 10, "network_type": "standard"}


class RouteTests(unittest.TestCase):
    def test_channel_9_is_a_remote_app(self):
        r = core.resolve(CH9)
        self.assertEqual((r.channel, r.kind, r.app, r.mode, r.backend),
                         (9, "remote_app", "stremio", core.REMOTE_APP_MODE, "remote_app"))

    def test_fs42_stations_stay_linear_mode(self):
        for ntype in ("standard", "web", "guide", "loop", "executable"):
            with self.subTest(ntype=ntype):
                r = core.resolve({**CH10, "network_type": ntype})
                self.assertEqual((r.kind, r.mode, r.backend), ("linear", core.LINEAR_MODE, f"fs42/{ntype}"))

    def test_router_state(self):
        st = core.router_state(core.resolve(CH9), 10, "active", None, 5.0)
        self.assertEqual(st["mode"], "REMOTE_APP_MODE")
        self.assertEqual((st["channel"], st["previous_channel"], st["app"], st["session"]), (9, 10, "stremio", "active"))


class RegistryTests(unittest.TestCase):
    def test_repo_registry_is_valid(self):
        apps = core.load_apps(json.loads((ROOT / "config/remote_apps.json").read_text()))
        self.assertEqual(apps["stremio"].provider, "sunshine")
        self.assertIsNone(apps["stremio"].ready_after_s)
        self.assertEqual(apps["testpattern"].ready_after_s, 1.5)

    def test_rejects_bad_entries(self):
        bad = [{}, {"remote_apps": []}, {"remote_apps": {"a": {"provider": "vnc"}}},
               {"remote_apps": {"a": {"provider": "command"}}},
               {"remote_apps": {"a": {"provider": "sunshine", "resolution": "big"}}},
               {"remote_apps": {"a": {"provider": "sunshine", "keys": {"Enter": ""}}}}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(core.ConfigError):
                core.load_apps(data)

    def test_load_app_explains_why_not(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "apps.json"
            with self.assertRaisesRegex(core.ConfigError, "unreadable"):
                core.load_app(p, "stremio")
            p.write_text("{nope")
            with self.assertRaisesRegex(core.ConfigError, "invalid"):
                core.load_app(p, "stremio")
            p.write_text(json.dumps({"remote_apps": {"stremio": {"provider": "sunshine", "application": "X"}}}))
            with self.assertRaisesRegex(core.ConfigError, "no Sunshine host"):
                core.load_app(p, "stremio")
            with self.assertRaisesRegex(core.ConfigError, "not in the registry"):
                core.load_app(p, "jellyfin")
            with self.assertRaisesRegex(core.ConfigError, "no wdbs_app"):
                core.load_app(p, None)

    def test_moonlight_argv_never_quits_the_host_app(self):
        app = core.load_apps({"remote_apps": {"s": {"provider": "sunshine", "host": "10.0.0.2",
                                                    "application": "WDBS Stremio", "fps": 30}}})["s"]
        argv = core.client_argv(app, "/ml")
        self.assertEqual(argv[:4], ["/ml", "stream", "10.0.0.2", "WDBS Stremio"])
        self.assertIn("--display-mode", argv)
        self.assertEqual(argv[argv.index("--fps") + 1], "30")
        self.assertNotIn("--quit-after", argv)

    def test_keys(self):
        app = core.load_apps({"remote_apps": {"s": {"provider": "sunshine", "keys": {"Back": "BackSpace"}}}})["s"]
        self.assertEqual(app.keysym("ArrowUp"), "Up")
        self.assertEqual(app.keysym("Back"), "BackSpace")      # per-app override
        self.assertEqual(app.keysym("PlayPause"), "space")
        self.assertIsNone(app.keysym("1"))                      # digits are never app keys


class SessionTests(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(core.classify_line("00:00:02 - SDL Info (0): Received first video packet after 81 ms"), "ready")
        self.assertEqual(core.classify_line("Starting WDBS Stremio failed: Error 503"), "failed")
        self.assertEqual(core.classify_line("Failed to connect to app-host.example.invalid"), "failed")
        self.assertEqual(core.classify_line("Connection terminated: -1"), "failed")
        self.assertEqual(core.classify_line("Starting video stream..."), "info")
        self.assertEqual(core.classify_line("Video stream is 1920x1080x60 (format 0x1)"), "info")
        self.assertIsNone(core.classify_line("Qt Info: whatever"))

    def test_connect_ready_drop(self):
        s = core.Session(10)
        s.start(0)
        s.line(1, "Starting video stream...")
        self.assertEqual(s.state, "connecting")
        s.line(2, "Received first video packet after 5 ms")
        self.assertEqual(s.state, "active")
        s.exited(50, 1)
        self.assertEqual(s.state, "failed")
        self.assertIn("connection lost", s.reason)

    def test_timeout_and_early_exit(self):
        s = core.Session(10)
        s.start(0)
        s.tick(9.9)
        self.assertEqual(s.state, "connecting")
        s.tick(10)
        self.assertEqual((s.state, s.reason), ("failed", "no picture after 10 s"))
        s = core.Session(10)
        s.start(0)
        s.exited(1, 2)
        self.assertIn("before the picture", s.reason)

    def test_command_provider_ready_after(self):
        s = core.Session(10, ready_after_s=1.5)
        s.start(0)
        s.tick(1.0)
        self.assertEqual(s.state, "connecting")
        s.tick(1.5)
        self.assertEqual(s.state, "active")

    def test_failure_slate_text(self):
        r = core.resolve(CH9)
        self.assertEqual(core.failure_lines(r, "no picture after 25 s")["headline"], "SERVICE TEMPORARILY UNAVAILABLE")
        self.assertEqual(core.failure_lines(r, "app 'stremio' has no Sunshine host/application yet")["headline"],
                         "SERVICE NOT SET UP YET")
        self.assertEqual(core.failure_lines(r, None)["channel"], "CHANNEL 9")


if __name__ == "__main__":
    unittest.main()
