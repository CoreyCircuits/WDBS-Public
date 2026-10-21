"""WDBS standby slates over FS42 playout problems (runs on the TV host as wdbs-slate.service).

A second client on FS42's mpv IPC socket (runtime/mpv.socket) - no FS42 patch. Every TICK it reads
mpv's path / time-pos / pause and shows one of three full-screen OSD overlays (Remotion stills,
wdbs-graphics "Standby" folder, raw BGRA at the screen size):

  start   - a new item has not started moving within DELAY_S (slow start, start-timeout retry)
  stall   - the item had been playing, then time-pos froze for DELAY_S (waiting on the drive)
  trouble - FS42 fell back to its standby image (runtime/standby.png)

The overlay goes away the moment time-pos moves again. Nothing is shown for web entries (the
web-cover black and the web window take the screen), an idle mpv (guide channel, tail hold), or
a user pause. The overlay only covers the picture; audio and the schedule are untouched.

    python -m wdbs.slate [--dry-run]          # --dry-run: log decisions, never touch the overlay
Disable: systemctl --user disable --now wdbs-slate.service (removes any overlay on exit).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("wdbs.slate")
TICK = 0.25
DELAY_S = float(os.environ.get("WDBS_SLATE_DELAY", "1.5"))
PLAYED_S = 1.0          # this much smooth playback makes a later freeze a "stall", not a slow start
OVERLAY_ID = 42
SLATES = {"start": "StandbyStart", "stall": "StandbyStall", "trouble": "StandbyTrouble"}

STATE = Path(os.environ.get("WDBS_SLATE_STATE", str(Path.home() / ".local/state/wdbs/slate.json")))


def publish(state_path: Path, slate: str | None, path: str | None = None) -> None:
    """Tell other WDBS tools (Remote v2 ratings) which slate is up. Atomic; never raises."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps({"slate": slate, "over": os.path.basename(path or ""), "at": time.time()}))
        os.replace(tmp, state_path)
    except OSError as exc:
        LOG.debug("slate state not written: %s", exc)


@dataclass
class Watch:
    """Pure decision logic: feed it samples, it says which slate (or None) should be up."""
    delay_s: float = DELAY_S
    path: str | None = None
    since: float = 0.0
    last_tp: float | None = None
    last_move: float = 0.0
    played: float = 0.0
    shown: str | None = field(default=None)

    def update(self, now: float, path: str | None, tp: float | None, paused: bool, idle: bool) -> str | None:
        if idle or not path:
            self._reset(None, now)
            return None
        if path.endswith("standby.png"):
            if path != self.path:
                self._reset(path, now)
            # FS42 flashes its standby image briefly at startup; only a lasting fallback is "trouble"
            return "trouble" if now - self.since >= self.delay_s else None
        if path.startswith(("av://", "lavfi", "http:", "https:", ":autobump:")) or "/runtime/" in path:
            self._reset(path, now)
            return None
        if path != self.path:
            self._reset(path, now)
        if tp is not None and self.last_tp is not None and tp != self.last_tp:
            d = tp - self.last_tp
            if 0 < d < 2.0:
                self.played += d
            self.last_move = now
        elif tp is not None and self.last_tp is None:
            self.last_move = now
        self.last_tp = tp
        if paused:
            return None
        if now - self.last_move >= self.delay_s:
            return "stall" if self.played >= PLAYED_S else "start"
        return None

    def _reset(self, path, now):
        self.path, self.since, self.last_tp, self.last_move, self.played = path, now, None, now, 0.0


def allowed(want: str | None, holding: bool) -> str | None:
    """While the tuning card holds a tune, the start slate would only duplicate it (stalls/trouble still show)."""
    return None if want == "start" and holding else want


def _tuner_holding(path: Path) -> bool:
    from wdbs.tuner_core import holding_now
    try:
        return holding_now(json.loads(path.read_text()), time.time())
    except (OSError, ValueError):
        return False


class MpvIpc:
    def __init__(self, sock_path: Path):
        self.sock_path = sock_path
        self.s: socket.socket | None = None
        self.buf = b""
        self.rid = 0

    def connect(self):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(str(self.sock_path))
        self.s, self.buf = s, b""
        # mpv pushes every event to every IPC client. This client only reads replies to its own
        # commands, so a caller that goes quiet (the tuner between tunes) stops draining the socket:
        # mpv's send queue fills and its IPC thread spins at 100% CPU (seen 2026-09-30). Opt out.
        self.command("disable_event", "all")

    def close(self):
        if self.s:
            try:
                self.s.close()
            finally:
                self.s = None

    def command(self, *args):
        self.rid += 1
        rid = self.rid
        self.s.sendall((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        while True:
            while b"\n" not in self.buf:
                chunk = self.s.recv(65536)
                if not chunk:
                    raise ConnectionError("mpv closed the socket")
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            msg = json.loads(line)
            if msg.get("request_id") == rid:
                return msg

    def get(self, prop):
        r = self.command("get_property", prop)
        return r.get("data") if r.get("error") == "success" else None


def run(fs42_root: Path, assets: Path, dry_run: bool = False, state: Path = STATE,
        tuner_state: Path | None = None):
    from wdbs.tuner_core import TUNER_STATE
    tuner_state = tuner_state or TUNER_STATE
    ipc = MpvIpc(fs42_root / "runtime" / "mpv.socket")

    def stop(*_):  # never leave a slate on screen when the service stops
        try:
            if ipc.s is None:
                ipc.connect()
            ipc.command("overlay-remove", OVERLAY_ID)
        except (OSError, ConnectionError, ValueError):
            pass
        if not dry_run:
            publish(state, None)
        raise SystemExit(0)

    import signal
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    watch = Watch()
    shown = None
    size = None
    if not dry_run:
        # WDBS: a clean start always begins with no slate recorded, so an unclean previous exit
        # (SIGKILL, OOM, power cut) can never leave a stale slate in slate.json.
        publish(state, None)
    while True:
        try:
            if ipc.s is None:
                ipc.connect()
                shown, size = None, None
                if not dry_run:
                    # A slate shown before an IPC drop is still on screen; we can't have removed it while the
                    # socket was down. Clear it so the screen matches shown=None (seen 2026-09-30 23:30: a start
                    # slate stayed up over a playing show after a 2 s IPC timeout).
                    ipc.command("overlay-remove", OVERLAY_ID)
                LOG.info("connected to %s", ipc.sock_path)
            now = time.monotonic()
            want = allowed(watch.update(now, ipc.get("path"), ipc.get("time-pos"), bool(ipc.get("pause")),
                                        bool(ipc.get("idle-active"))), _tuner_holding(tuner_state))
            if want != shown:
                if dry_run:
                    LOG.info("would %s", f"show {want}" if want else "hide")
                elif want:
                    size = size or (ipc.get("osd-width"), ipc.get("osd-height"))
                    raw = assets / f"{SLATES[want]}.bgra"
                    w, h = 1920, 1080
                    if size != (w, h):
                        LOG.warning("screen is %sx%s, slates are %dx%d; showing unscaled", *size, w, h)
                    r = ipc.command("overlay-add", OVERLAY_ID, 0, 0, str(raw), 0, "bgra", w, h, w * 4)
                    LOG.info("show %s (%s) over %s", want, r.get("error"), os.path.basename(watch.path or ""))
                else:
                    ipc.command("overlay-remove", OVERLAY_ID)
                    LOG.info("hide")
                shown = want
                if not dry_run:
                    publish(state, want, watch.path)
            time.sleep(TICK)
        except (OSError, ConnectionError, ValueError) as exc:
            if ipc.s is not None:
                LOG.warning("mpv IPC lost (%s); retrying", exc)
            ipc.close()
            shown = None
            if not dry_run:
                publish(state, None)
            time.sleep(2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wdbs slate")
    ap.add_argument("--fs42-root", default=os.environ.get("WDBS_FS42_ROOT", str(Path.home() / "FieldStation42")))
    ap.add_argument("--assets", default=None, help="dir with Standby*.bgra (default <fs42>/wdbs_assets/standby)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(a.fs42_root)
    assets = Path(a.assets) if a.assets else root / "wdbs_assets" / "standby"
    missing = [n for n in SLATES.values() if not (assets / f"{n}.bgra").exists()]
    if missing and not a.dry_run:
        raise SystemExit(f"missing slate files in {assets}: {missing}")
    run(root, assets, a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
