"""WDBS remote-app channel controller (fs42_overlay/wdbs_remote_app.py) against a fake Moonlight and a fake player.
Real processes and real time (about 10 s total); no X display, no FieldStation42 needed."""

import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"fs42.{name}", ROOT / "fs42_overlay" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"fs42.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


fs42_pkg = sys.modules.setdefault("fs42", types.ModuleType("fs42"))
fs42_pkg.wdbs_router_core = _load("wdbs_router_core")
ra = _load("wdbs_remote_app")

FAKE_MOONLIGHT = """#!/usr/bin/env python3
import sys, time
host = sys.argv[2]
print("00:00:00 - Starting video stream...", flush=True)
if host == "down":
    print("00:00:01 - Failed to connect to down", flush=True); time.sleep(60)
elif host == "silent":
    time.sleep(60)
else:
    time.sleep(0.2)
    print("00:00:01 - SDL Info (0): Received first video packet after 42 ms", flush=True)
    time.sleep(0.6 if host == "crash" else 60)
    sys.exit(3)
"""


class Out:
    def __init__(self, status, payload=None):
        self.status, self.payload = status, payload


CHANGE = Out("CHANNEL_CHANGE", '{"command": "direct", "channel": 10}')


class FakeMpv:
    def __init__(self):
        self.calls = []

    def command(self, *a):
        self.calls.append(a)
        return 1 if a[:2] == ("get_property", "image-display-duration") else None

    def play(self, what):
        self.calls.append(("play", what))


class FakePlayer:
    def __init__(self):
        self.mpv, self.current_playing_file_path = FakeMpv(), "x"

    def stop_player(self):
        pass

    def handle_runtime_command_outcome(self, r):
        return isinstance(r.payload, str) and r.payload.startswith("mpv_command:")

    @staticmethod
    def is_non_interrupting(r):
        return r.status == "SUCCESS"


class FakeKeys:
    sent = []

    def tap(self, sym):
        FakeKeys.sent.append(sym)

    def close(self):
        pass


class Script:
    """input_check that plays (seconds after start, outcome) events once each."""

    def __init__(self, events):
        self.t0, self.events = time.monotonic(), list(events)

    def __call__(self):
        if self.events and time.monotonic() - self.t0 >= self.events[0][0]:
            return self.events.pop(0)[1]
        return None


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.ml = d / "moonlight"
        self.ml.write_text(FAKE_MOONLIGHT)
        self.ml.chmod(0o755)
        self.runtime = d / "runtime"
        self.runtime.mkdir()
        ra.ROUTER_STATE = d / "router.json"
        ra._track.update(current=None, previous=None, loaded=True)
        FakeKeys.sent = []
        self.focused, self.slates = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def channel(self, host, events, app_extra=None):
        apps = Path(self.tmp.name) / "apps.json"
        app = {"provider": "sunshine", "host": host, "application": "WDBS Stremio", "connect_timeout_s": 2,
               **(app_extra or {})}
        apps.write_text(json.dumps({"remote_apps": {"stremio": app}}))
        conf = json.loads((ROOT / "config/stations/wdbs_stream.json").read_text())["station_conf"]
        ra.on_tune(conf)
        self.player = FakePlayer()

        def slate(route, reason, out):
            self.slates.append(reason)
            return out

        ch = ra.RemoteAppChannel(self.player, conf, Script(events), status_fn=lambda r, t=None: None, apps_file=apps,
                                 moonlight=str(self.ml), keys_factory=FakeKeys, focus_fn=self.focused.append,
                                 slate_fn=slate, runtime=self.runtime)
        return ch, ch.run()

    def ready_url(self):
        return json.loads((self.runtime / "wdbs_web_ready").read_text())["url"]

    def test_connect_keys_and_leave(self):
        events = [(0.05, Out("SUCCESS", "web_key:ArrowUp")),            # still connecting: dropped
                  (1.2, Out("SUCCESS", "web_key:ArrowUp")), (1.3, Out("SUCCESS", "web_key:Enter")),
                  (1.4, Out("SUCCESS", "web_key:PlayPause")), (1.5, Out("SUCCESS", "web_key:Bogus")),
                  (1.6, Out("SUCCESS", "mpv_command:vol")), (1.8, CHANGE)]
        ch, out = self.channel("ok", events)
        self.assertIs(out, CHANGE)                                        # the channel command goes back to FS42
        self.assertEqual(FakeKeys.sent, ["Up", "Return", "space"])
        self.assertEqual(len(self.focused), 1)
        self.assertEqual(self.ready_url(), "wdbs-app:9")
        self.assertIsNone(ch.client)
        self.assertEqual(json.loads(ra.ROUTER_STATE.read_text())["session"], "active")
        self.assertEqual(self.slates, [])
        self.assertEqual(self.player.mpv.calls[-1], ("play", "av://lavfi:color=c=black:s=1280x720:r=10"))

    def test_host_down_then_retry_then_leave(self):
        events = [(0.8, Out("SUCCESS", "web_key:ArrowUp")),                # on the slate: ignored
                  (1.0, Out("SUCCESS", "web_key:Enter")), (2.0, CHANGE)]  # OK = retry (fails again)
        ch, out = self.channel("down", events)
        self.assertIs(out, CHANGE)
        self.assertEqual(len(self.slates), 2)
        self.assertIn("Failed to connect", self.slates[0])
        self.assertEqual(FakeKeys.sent, [])
        self.assertEqual(self.ready_url(), "wdbs-app:9")                 # the tuner releases onto the slate
        self.assertIn(("set_property", "image-display-duration", "inf"), self.player.mpv.calls)
        self.assertEqual(self.player.mpv.calls[-3], ("set_property", "image-display-duration", 1))  # restored
        st = json.loads(ra.ROUTER_STATE.read_text())
        self.assertEqual((st["session"], st["mode"]), ("failed", "REMOTE_APP_MODE"))

    def test_crash_after_picture(self):
        ch, out = self.channel("crash", [(2.5, CHANGE)])
        self.assertIn("connection lost", self.slates[0])

    def test_no_picture_times_out(self):
        ch, out = self.channel("silent", [(2.8, CHANGE)])
        self.assertEqual(self.slates, ["no picture after 2 s"])

    def test_unconfigured_host_fails_at_once(self):
        ch, out = self.channel("", [(0.3, CHANGE)])
        self.assertIn("no Sunshine host", self.slates[0])

    def test_client_is_really_stopped(self):
        pids = []
        orig = ra.Client.__init__

        def spy(self_, argv):
            orig(self_, argv)
            pids.append(self_.proc.pid)
        ra.Client.__init__ = spy
        try:
            self.channel("ok", [(0.8, CHANGE)])
        finally:
            ra.Client.__init__ = orig
        time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pids[0], 0)


class TrackTests(unittest.TestCase):
    def test_previous_channel_across_backends(self):
        with tempfile.TemporaryDirectory() as d:
            ra.ROUTER_STATE = Path(d) / "router.json"
            ra._track.update(current=None, previous=None, loaded=True)
            ch9 = json.loads((ROOT / "config/stations/wdbs_stream.json").read_text())["station_conf"]
            ch10 = {"network_name": "EXAMPLE SITCOMS", "channel_number": 10, "network_type": "standard"}
            for conf in (ch10, ch9):
                ra.on_tune(conf)
            st = json.loads(ra.ROUTER_STATE.read_text())
            self.assertEqual((st["mode"], st["channel"], st["previous_channel"]), ("REMOTE_APP_MODE", 9, 10))
            ra.on_tune(ch10)
            st = json.loads(ra.ROUTER_STATE.read_text())
            self.assertEqual((st["mode"], st["channel"], st["previous_channel"], st["backend"]),
                             ("LINEAR_MODE", 10, 9, "fs42/standard"))


if __name__ == "__main__":
    unittest.main()
