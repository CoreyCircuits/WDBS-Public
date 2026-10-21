import json
import os
import unittest
from pathlib import Path

from wdbs.slate import Watch, allowed, _tuner_holding

EP = "/path/to/FieldStation42/catalog/example_sitcoms/community/Community.S01E08.mkv"
AD = "/path/to/FieldStation42/catalog/example_sitcoms/commercial/WDBS__x.mp4"


def run(w, samples):
    """samples: (t, path, time_pos, paused, idle) -> list of decisions"""
    return [w.update(t, p, tp, pa, idle) for t, p, tp, pa, idle in samples]


class SlateWatchTests(unittest.TestCase):
    def test_quick_start_shows_nothing(self):
        w = Watch(delay_s=1.5)
        out = run(w, [(0, EP, None, False, False), (0.5, EP, 0.0, False, False), (1.0, EP, 0.5, False, False),
                      (1.5, EP, 1.0, False, False), (2.0, EP, 1.5, False, False)])
        self.assertEqual(set(out), {None})

    def test_slow_start_shows_start_then_hides_when_it_moves(self):
        w = Watch(delay_s=1.5)
        out = run(w, [(0, AD, None, False, False), (1.0, AD, None, False, False), (1.6, AD, None, False, False),
                      (5.0, AD, 0.0, False, False), (5.25, AD, 0.25, False, False)])
        self.assertEqual(out[2], "start")
        self.assertEqual(out[3:], [None, None])

    def test_seek_wait_after_load_is_still_a_start(self):
        w = Watch(delay_s=1.5)   # load, jump to the offset, then wait for the seek to land
        out = run(w, [(0, EP, None, False, False), (0.3, EP, 0.0, False, False), (0.6, EP, 956.2, False, False),
                      (2.2, EP, 956.2, False, False)])
        self.assertEqual(out[-1], "start")

    def test_freeze_mid_program_is_a_stall(self):
        w = Watch(delay_s=1.5)
        s = [(t / 4, EP, 100 + t / 4, False, False) for t in range(0, 12)]          # 3 s of playback
        s += [(3.0 + t / 4, EP, 102.75, False, False) for t in range(1, 8)]          # frozen 1.75 s
        s += [(5.0, EP, 103.0, False, False)]
        out = run(w, s)
        self.assertIn("stall", out)
        self.assertNotIn("start", out)
        self.assertIsNone(out[-1])

    def test_user_pause_and_web_and_idle_show_nothing(self):
        w = Watch(delay_s=1.5)
        self.assertEqual(set(run(w, [(t, EP, 50.0, True, False) for t in range(5)])), {None})
        w = Watch(delay_s=1.5)
        self.assertEqual(set(run(w, [(t, "av://lavfi:color=c=black:s=1280x720:r=10", None, False, False)
                                     for t in range(5)])), {None})
        w = Watch(delay_s=1.5)
        self.assertEqual(set(run(w, [(t, None, None, False, True) for t in range(5)])), {None})

    def test_lasting_standby_image_is_trouble_brief_flash_is_not(self):
        w = Watch(delay_s=1.5)
        self.assertIsNone(w.update(0, "runtime/standby.png", None, False, False))
        self.assertIsNone(w.update(0.5, "runtime/standby.png", None, False, False))
        self.assertEqual(w.update(1.6, "runtime/standby.png", None, False, False), "trouble")
        self.assertIsNone(w.update(2.0, EP, None, False, False))       # recovered: next item loading

    def test_new_item_resets(self):
        w = Watch(delay_s=1.5)
        run(w, [(t / 4, EP, 10 + t / 4, False, False) for t in range(12)])
        self.assertIsNone(w.update(3.1, AD, None, False, False))      # next item just loaded
        self.assertEqual(w.update(4.7, AD, None, False, False), "start")


import json
import signal
import tempfile
import time
from pathlib import Path
from unittest import mock

import wdbs.slate as slate_mod
from wdbs.slate import publish


class SlatePublishTests(unittest.TestCase):
    def test_publish_writes_then_clears(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state" / "slate.json"
            publish(p, "stall", "/x/Community.S01E08.mkv")
            d = json.loads(p.read_text())
            self.assertEqual((d["slate"], d["over"]), ("stall", "Community.S01E08.mkv"))
            publish(p, None)
            self.assertIsNone(json.loads(p.read_text())["slate"])

    def test_publish_never_raises(self):
        publish(Path("/proc/wdbs-cannot-write/slate.json"), "start")   # logged, not raised


class _Stop(Exception):
    """Sentinel used to unwind run()'s infinite loop once a test has seen what it needs."""


class RunPublishGuardTests(unittest.TestCase):
    """run() talks to mpv over a real unix socket, so it is not exercised end-to-end here - but
    MpvIpc is a small enough seam to fake, which lets these tests drive run() itself (startup, the
    reconnect/except path, and the SIGTERM/SIGINT handler) without any real mpv or OS signal."""

    def _state(self, td, initial="stall"):
        p = Path(td) / "slate.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"slate": initial, "over": "x", "at": 0}))
        return p

    def test_startup_and_reconnect_publish_none_unless_dry_run(self):
        # Every connect() fails once (driving the except path), then aborts the test on the retry -
        # covering both the pre-loop startup publish (M4) and the except-path publish (T2).
        class FakeIpc:
            def __init__(self, sock_path):
                self.s = None
                self.calls = 0

            def connect(self):
                self.calls += 1
                if self.calls == 1:
                    self.s = object()
                    raise OSError("boom")
                raise _Stop()

            def close(self):
                self.s = None

        for dry_run, expect_none in ((False, True), (True, False)):
            with self.subTest(dry_run=dry_run):
                with tempfile.TemporaryDirectory() as td:
                    state = self._state(td)
                    with mock.patch.object(slate_mod, "MpvIpc", FakeIpc), \
                         mock.patch.object(slate_mod.time, "sleep", lambda s: None), \
                         mock.patch("signal.signal"):  # never install a real handler for this test
                        with self.assertRaises(_Stop):
                            slate_mod.run(Path("/nowhere"), Path("/nowhere"), dry_run=dry_run, state=state)
                    slate = json.loads(state.read_text())["slate"]
                    self.assertEqual(slate is None, expect_none)

    def test_reconnect_clears_a_slate_left_on_screen(self):
        # After an IPC drop the slate we showed is still on screen; a reconnect must remove it, not just forget it.
        sent = []

        class FakeIpc:
            def __init__(self, sock_path):
                self.s = None
                self.sock_path = sock_path

            def connect(self):
                self.s = object()

            def close(self):
                self.s = None

            def command(self, *args):
                sent.append(args)
                return {"error": "success"}

            def get(self, prop):
                raise _Stop()

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(slate_mod, "MpvIpc", FakeIpc), \
                 mock.patch.object(slate_mod.time, "sleep", lambda s: None), \
                 mock.patch("signal.signal"):
                with self.assertRaises(_Stop):
                    slate_mod.run(Path("/nowhere"), Path("/nowhere"), dry_run=False, state=self._state(td))
        self.assertIn(("overlay-remove", slate_mod.OVERLAY_ID), sent)

    def test_stop_handler_publishes_none_unless_dry_run(self):
        # Capture the SIGTERM handler run() registers and call it directly (no real signal delivery),
        # simulating the service being stopped mid-tick.
        for dry_run, expect_none in ((False, True), (True, False)):
            with self.subTest(dry_run=dry_run):
                handlers = {}

                class FakeIpc:
                    def __init__(self, sock_path):
                        self.s = None
                        self.sock_path = sock_path

                    def connect(self):
                        self.s = object()

                    def close(self):
                        self.s = None

                    def command(self, *args):
                        return {}

                    def get(self, prop):
                        if prop == "path":
                            handlers[signal.SIGTERM]()  # simulate SIGTERM arriving mid-tick
                        return None

                with tempfile.TemporaryDirectory() as td:
                    state = self._state(td)
                    with mock.patch.object(slate_mod, "MpvIpc", FakeIpc), \
                         mock.patch("signal.signal", side_effect=lambda sig, h: handlers.setdefault(sig, h)):
                        with self.assertRaises(SystemExit):
                            slate_mod.run(Path("/nowhere"), Path("/nowhere"), dry_run=dry_run, state=state)
                    slate = json.loads(state.read_text())["slate"]
                    self.assertEqual(slate is None, expect_none)


class TunerHoldTests(unittest.TestCase):
    def test_start_slate_waits_for_the_tuner(self):
        self.assertIsNone(allowed("start", holding=True))
        self.assertEqual(allowed("start", holding=False), "start")
        self.assertEqual(allowed("stall", holding=True), "stall")
        self.assertEqual(allowed("trouble", holding=True), "trouble")
        self.assertIsNone(allowed(None, holding=True))

    def test_tuner_holding_reads_and_handles_errors(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            # Missing file
            self.assertFalse(_tuner_holding(state_dir / "missing.json"))
            # Invalid JSON
            (state_dir / "invalid.json").write_text("not json at all")
            self.assertFalse(_tuner_holding(state_dir / "invalid.json"))
            # Non-dict JSON (true)
            (state_dir / "true.json").write_text("true")
            self.assertFalse(_tuner_holding(state_dir / "true.json"))
            # Non-dict JSON ([1, 2])
            (state_dir / "list.json").write_text("[1, 2]")
            self.assertFalse(_tuner_holding(state_dir / "list.json"))
            # Valid holding state
            (state_dir / "valid.json").write_text(json.dumps({"holding": True, "channel": 10, "since": time.time()}))
            self.assertTrue(_tuner_holding(state_dir / "valid.json"))


if __name__ == "__main__":
    unittest.main()


class MpvIpcTests(unittest.TestCase):
    def test_connect_opts_out_of_events_and_skips_them(self):
        import socket as sk, tempfile, threading
        from wdbs.slate import MpvIpc
        d = tempfile.mkdtemp()
        path = os.path.join(d, "mpv.socket")
        srv = sk.socket(sk.AF_UNIX)
        srv.bind(path)
        srv.listen(1)
        seen = []

        def serve():
            c, _ = srv.accept()
            f = c.makefile("rb")
            for line in f:
                msg = json.loads(line)
                seen.append(msg["command"])
                reply = {"request_id": msg["request_id"], "error": "success"}
                if msg["command"][0] == "get_property":
                    reply["data"] = 1.5
                c.sendall(b'{"event":"playback-restart"}\n' + json.dumps(reply).encode() + b"\n")
        threading.Thread(target=serve, daemon=True).start()
        ipc = MpvIpc(Path(path))
        ipc.connect()
        self.assertEqual(ipc.get("time-pos"), 1.5)
        ipc.close()
        srv.close()
        self.assertEqual(seen[0], ["disable_event", "all"])
