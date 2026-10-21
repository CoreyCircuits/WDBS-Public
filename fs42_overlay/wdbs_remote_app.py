"""WDBS remote-app backend for FieldStation42 (installed as fs42/wdbs_remote_app.py by the remote-app patch).
Decisions live in fs42/wdbs_router_core.py; this file runs them inside the player's main loop.

While a remote-app channel (Channel 9 WDBS STREAM) is on:
  * FS42 keeps running. Its mpv shows a silent black cover (the same trick as web channels) under the client
    window, so the desktop never shows and there is exactly one audio source (the client's). Linear channels
    are wall-clock scheduled, so returning to them resumes at the scheduled position.
  * The client (Moonlight -> Sunshine on the app host) runs as a child of this process. Leaving the channel stops
    the client only; the host app keeps running, so Stremio's state survives (no --quit-after).
  * Remote v2 keys (key:ArrowUp ... key:PlayPause) arrive as FS42 web_key outcomes and are typed into the
    client window with XTest. Digits, CH+/CH-, GUIDE, BACK, INFO stay WDBS commands: they end this loop (or,
    for INFO, go to wdbs-tuner) and never reach the app.
  * Failure (no host, not paired, crash, dropped stream, no picture in time) -> the client is stopped and a
    WDBS slate is shown in mpv. OK retries; any channel command leaves.
Log: journalctl --user -u fs42-player | grep WDBS.router   State: ~/.local/state/wdbs/router.json
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path

from fs42 import wdbs_router_core as core

LOG = logging.getLogger("WDBS.router")
HOME = Path.home()
STATE_DIR = Path(os.environ.get("WDBS_STATE_DIR", str(HOME / ".local/state/wdbs")))
ROUTER_STATE = STATE_DIR / "router.json"
APPS_FILE = Path(os.environ.get("WDBS_REMOTE_APPS", str(HOME / "wdbs/config/remote_apps.json")))
MOONLIGHT = os.environ.get("WDBS_MOONLIGHT", str(HOME / ".local/bin/moonlight"))
KEY_TOKEN = Path(os.environ.get("WDBS_KEYRELAY_TOKEN", str(HOME / ".config/wdbs/keyrelay.token")))
RUNTIME = Path("runtime")                  # FS42 runs from its own root
READY_DELAY_S = 0.5                        # first video packet -> first decoded frame on screen
STOP_GRACE_S = 1.0
_track = {"current": None, "previous": None, "loaded": False}


# ---------------------------------------------------------------- router state (every tune)
def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _load_track() -> None:
    if _track["loaded"]:
        return
    _track["loaded"] = True
    try:
        st = json.loads(ROUTER_STATE.read_text())
        _track.update(current=st.get("channel"), previous=st.get("previous_channel"))
    except (OSError, ValueError, AttributeError):
        pass


def publish(route: core.Route, session: str | None = None, reason: str | None = None) -> None:
    try:
        _write_json(ROUTER_STATE, core.router_state(route, _track["previous"], session, reason, time.time()))
    except OSError:
        LOG.exception("router state not written")


def on_tune(channel_conf: dict) -> core.Route:
    """Every channel change (called from field_player right after the tune event). Never raises."""
    _load_track()
    route = core.resolve(channel_conf)
    if route.channel != _track["current"]:
        if _track["current"] is not None:
            _track["previous"] = _track["current"]
        _track["current"] = route.channel
    LOG.info("tune: %s %s -> backend %s, mode %s (previous %s)", route.channel, route.name, route.backend,
             route.mode, _track["previous"])
    publish(route, "connecting" if route.kind == "remote_app" else None)
    return route


# ---------------------------------------------------------------- keys into the client window (XTest)
class XKeys:
    """Types keysyms with XTest into whatever window has focus (the client, which we activate)."""

    def __init__(self):
        self.x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        self.xtst = ctypes.cdll.LoadLibrary("libXtst.so.6")
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XStringToKeysym.restype = ctypes.c_ulong
        self.x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.dpy = self.x11.XOpenDisplay(None)
        if not self.dpy:
            raise OSError("cannot open X display")

    def tap(self, keysym: str) -> None:
        ks = self.x11.XStringToKeysym(keysym.encode())
        kc = self.x11.XKeysymToKeycode(self.dpy, ks) if ks else 0
        if not kc:
            raise ValueError(f"no keycode for {keysym!r}")
        self.xtst.XTestFakeKeyEvent(self.dpy, kc, 1, 0)
        self.xtst.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
        self.x11.XFlush(self.dpy)

    def close(self) -> None:
        if self.dpy:
            self.x11.XCloseDisplay(self.dpy)
            self.dpy = None


class RelayKeys:
    """Keys to the app host's WDBS key relay (prodesk/apphost/keyrelay.py): HTTP POST with the shared token, from
    one worker thread so the player loop never waits on the network. Keys keep their order; a backlog is dropped."""

    MAX_BACKLOG = 8

    def __init__(self, url: str, token_file: Path = KEY_TOKEN, timeout_s: float = 1.0):
        st = token_file.stat()
        if st.st_mode & 0o077:
            raise OSError(f"{token_file} must be mode 600")
        self.url, self.token, self.timeout_s = url, token_file.read_text().strip(), timeout_s
        self.q: queue.Queue = queue.Queue()
        self.sent, self.failed = 0, 0
        threading.Thread(target=self._work, daemon=True).start()

    def tap(self, keysym: str) -> None:
        if self.q.qsize() >= self.MAX_BACKLOG:
            raise ValueError("key relay backlog full")
        self.q.put(keysym)

    def _work(self) -> None:
        import urllib.error
        import urllib.request
        while True:
            sym = self.q.get()
            if sym is None:
                return
            req = urllib.request.Request(self.url, data=json.dumps({"key": sym}).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-WDBS-Token": self.token})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    r.read()
                self.sent += 1
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.failed += 1
                LOG.warning("key relay: %s not delivered (%s)", sym, e)

    def close(self) -> None:
        self.q.put(None)


def focus_client(pgid: int) -> bool:
    """Activate the client's newest window (EWMH, via wmctrl) so XTest keys land in it. Windows are matched by
    process group, so the AppImage's child process and any client binary work, and FS42's own mpv never does."""
    try:
        out = subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True, timeout=2).stdout
        wids = []
        for ln in out.splitlines():
            parts = ln.split()
            try:
                if len(parts) > 2 and os.getpgid(int(parts[2])) == pgid:
                    wids.append(parts[0])
            except (ValueError, ProcessLookupError, PermissionError):
                continue
        if wids:
            subprocess.run(["wmctrl", "-i", "-a", wids[-1]], timeout=2)
            return True
        LOG.warning("no client window to focus yet")
    except (OSError, subprocess.SubprocessError):
        LOG.warning("could not focus the client window")
    return False


# ---------------------------------------------------------------- failure slate (WDBS kit, rendered once)
def render_slate(route: core.Route, reason: str | None, out: Path, fs42_root: Path = Path(".")) -> Path | None:
    """Full-screen WDBS 'service unavailable' still. Uses the logo kit PNG and the WDBS fonts; WDBS is never
    typed as text (logo rules, . Returns None if Pillow is missing (mpv then keeps the black cover)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    lines = core.failure_lines(route, reason)
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (42, 66, 65))                                  # Japanese Indigo
    d = ImageDraw.Draw(img)
    for y in range(H):                                                          # soft Dusk Blue lift
        a = max(0.0, 1 - abs(y - H * 0.42) / (H * 0.6))
        d.line([(0, y), (W, y)], fill=(int(42 + (32 - 42) * a), int(66 + (98 - 66) * a), int(65 + (137 - 65) * a)))
    fonts = fs42_root / "wdbs_assets/era/fonts"

    def font(name, size):
        try:
            return ImageFont.truetype(str(fonts / name), size)
        except OSError:
            return ImageFont.load_default()

    logo = fs42_root / "fs42/fs42_server/static/wdbsguide/img/wdbs_00s.png"
    y = 70
    if logo.exists():
        mark = Image.open(logo).convert("RGBA")
        mark.thumbnail((360, 140))
        img.paste(mark, ((W - mark.width) // 2, y), mark)
        y += mark.height + 40
    else:
        y += 150

    def center(text, f, fill, yy):
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, yy), text, font=f, fill=fill)

    center(lines["channel"], font("BarlowCondensed-SemiBold.ttf", 40), (221, 155, 81), y)          # Honeycomb
    # the station name in Barlow, never the wordmark's face (logo rules: WDBS itself only ever comes from the kit)
    center(lines["name"], font("BarlowCondensed-SemiBold.ttf", 84), (246, 241, 232), y + 44)       # Warm White
    d.rounded_rectangle([(190, y + 170), (W - 190, y + 250)], radius=16, fill=(48, 35, 55))      # Bleached Cedar
    center(lines["headline"], font("BarlowCondensed-SemiBold.ttf", 50), (246, 241, 232), y + 180)
    hint_font, key_font = font("BarlowCondensed-SemiBold.ttf", 36), font("TitanOne-Regular.ttf", 30)
    hy, x = y + 310, 300
    for key, label in lines["hints"]:
        kw = d.textlength(key, font=key_font) + 36
        d.rounded_rectangle([(x, hy), (x + kw, hy + 52)], radius=12, fill=(191, 100, 117))        # Lotus
        d.text((x + 18, hy + 8), key, font=key_font, fill=(246, 241, 232))
        d.text((x + kw + 16, hy + 6), label, font=hint_font, fill=(246, 241, 232))
        x += kw + 16 + d.textlength(label, font=hint_font) + 90
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp.png")
    img.save(tmp)
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------- the channel loop
class Client:
    """The client process (own process group) and a reader thread feeding its output lines to a queue."""

    def __init__(self, argv: list[str]):
        self.lines: queue.Queue[str] = queue.Queue()
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, errors="replace", bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.proc.stdout.close()

    def poll(self):
        return self.proc.poll()

    def stop(self) -> None:
        """Stop the client only (never the host app). SIGTERM the group, SIGKILL after STOP_GRACE_S."""
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=2)
        except ProcessLookupError:
            pass


class NowPlaying:
    """What the app host is playing, for INFO and the phone's TV line (owner 9/29): polls the key relay's read-only
    /nowplaying (same token, TV host only) every POLL_S from one worker thread; `title` is None while browsing."""

    POLL_S = 5.0

    def __init__(self, relay_key_url: str, token_file: Path = KEY_TOKEN):
        self.url = relay_key_url.rsplit("/", 1)[0] + "/nowplaying"
        self.token = token_file.read_text().strip()
        self.title: str | None = None
        self._stop = threading.Event()
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        import urllib.error
        import urllib.request
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(self.url, headers={"X-WDBS-Token": self.token})
                with urllib.request.urlopen(req, timeout=2) as r:
                    data = json.load(r)
                playing = data.get("state") in ("starting", "playing")
                self.title = (data.get("title") or None) if playing else None
            except (urllib.error.URLError, OSError, ValueError):
                pass                                  # keep the last title; the relay being away is not news
            self._stop.wait(self.POLL_S)

    def close(self) -> None:
        self._stop.set()


class RemoteAppChannel:
    def __init__(self, player, channel_conf: dict, input_check, *, status_fn, apps_file: Path = APPS_FILE,
                 moonlight: str = MOONLIGHT, keys_factory=XKeys, focus_fn=focus_client, slate_fn=render_slate,
                 runtime: Path = RUNTIME, clock=time.monotonic, sleep=time.sleep):
        self.player, self.conf, self.input_check = player, channel_conf, input_check
        self.status_fn, self.apps_file, self.moonlight = status_fn, apps_file, moonlight
        self.keys_factory, self.focus_fn, self.slate_fn = keys_factory, focus_fn, slate_fn
        self.runtime, self.clock, self.sleep = runtime, clock, sleep
        self.route = core.resolve(channel_conf)
        self.app: core.RemoteApp | None = None
        self.session = core.Session()
        self.client: Client | None = None
        self.keys = None
        self.ready_at: float | None = None
        self.image_duration = None
        self.now_playing: NowPlaying | None = None
        self.shown_title: str | None = None

    # -- presentation
    def _cover(self) -> None:
        """Silent black under the client window (no desktop between windows, no FS42 audio)."""
        try:
            self.player.mpv.command("playlist-clear")
            self.player.mpv.play("av://lavfi:color=c=black:s=1280x720:r=10")
        except Exception:
            self.player.stop_player()
        self.player.current_playing_file_path = None

    def _show_slate(self) -> None:
        out = self.runtime.resolve() / f"wdbs_app_unavailable_{self.route.channel}.png"
        try:
            path = self.slate_fn(self.route, self.session.reason, out)
            if path:
                if self.image_duration is None:
                    self.image_duration = self.player.mpv.command("get_property", "image-display-duration")
                self.player.mpv.command("set_property", "image-display-duration", "inf")
                self.player.mpv.play(str(path))
        except Exception:
            LOG.exception("unavailable slate not shown (black cover stays)")

    def _restore_mpv(self) -> None:
        if self.image_duration is not None:
            try:
                self.player.mpv.command("set_property", "image-display-duration", self.image_duration)
            except Exception:
                LOG.warning("image-display-duration not restored")
            self.image_duration = None

    def _signal_ready(self) -> None:
        """Release the tuning card (wdbs-tuner waits for this like a web page load)."""
        try:
            _write_json(self.runtime / "wdbs_web_ready", {"url": core.ready_url(self.route.channel), "at": time.time()})
        except OSError:
            LOG.warning("tuner ready signal not written")

    # -- session
    def _connect(self) -> None:
        now = self.clock()
        try:
            self.app = core.load_app(self.apps_file, self.route.app)
            self.session = core.Session(self.app.connect_timeout_s, self.app.ready_after_s)
            argv = core.client_argv(self.app, self.moonlight)
            self.client = Client(argv)
            self.session.start(now)
            LOG.info("remote app: %s (%s via %s %s) session: connecting", self.route.app, self.app.name,
                     self.app.provider, self.app.host or argv[0])
            publish(self.route, "connecting")
        except (core.ConfigError, OSError, ValueError) as e:
            self.session.fail(now, str(e))
            self._on_failed()

    def _on_active(self) -> None:
        LOG.info("session: active, mode: %s, audio: remote app (FS42 on silent cover)", core.REMOTE_APP_MODE)
        publish(self.route, "active")
        if self.app and self.app.key_relay and self.now_playing is None:
            try:
                self.now_playing = NowPlaying(self.app.key_relay)
            except OSError as e:
                LOG.warning("now playing unavailable: %s", e)
        self.ready_at = self.clock() + READY_DELAY_S
        if self.client:
            self.focus_fn(self.client.proc.pid)       # start_new_session: the client's pid is its group id

    def _on_failed(self) -> None:
        LOG.warning("session: failed (%s) -> WDBS unavailable slate", self.session.reason)
        if self.client:
            self.client.stop()
            self.client = None
        self._close_keys()
        self._show_slate()
        publish(self.route, "failed", self.session.reason)
        self._signal_ready()

    def _close_keys(self) -> None:
        if self.keys:
            self.keys.close()
            self.keys = None

    def _key(self, name: str) -> None:
        if self.session.state == "failed":
            if name == "Enter":
                LOG.info("retry requested")
                self._cover()
                self._connect()
            return
        if self.session.state != "active" or not self.app:
            return                                   # still connecting: drop it, don't queue blind input
        sym = self.app.keysym(name)
        if not sym:
            LOG.info("key %s has no mapping for %s", name, self.route.app)
            return
        try:
            if self.keys is None:
                self.keys = RelayKeys(self.app.key_relay) if self.app.key_relay else self.keys_factory()
            self.keys.tap(sym)
        except (OSError, ValueError) as e:
            LOG.warning("key %s -> %s not sent: %s", name, sym, e)
            self._close_keys()

    def _pump(self) -> None:
        if not self.client:
            return
        before = self.session.state
        while True:
            try:
                line = self.client.lines.get_nowait()
            except queue.Empty:
                break
            kind = self.session.line(self.clock(), line)
            if kind in ("ready", "failed", "info"):
                LOG.info("client: %s", line.strip()[:200])
        code = self.client.poll()
        if code is not None and self.session.state in ("connecting", "active") and self.client.lines.empty():
            self.session.exited(self.clock(), code)
        self.session.tick(self.clock())
        if self.session.state != before:
            if self.session.state == "active":
                self._on_active()
            elif self.session.state == "failed":
                self._on_failed()

    def _title_tick(self) -> None:
        """Mirror the app's now-playing title into FS42's status (INFO banner, phone TV line, recorder)."""
        title = self.now_playing.title if self.now_playing else None
        if title != self.shown_title:
            self.shown_title = title
            LOG.info("now playing: %s", title or "(browsing)")
            try:
                self.status_fn(self.route, title)
            except Exception:
                LOG.exception("status title not updated")

    def leave(self, why: str) -> None:
        LOG.info("leaving remote app %s (%s)", self.route.app, why)
        if self.now_playing:
            self.now_playing.close()
            self.now_playing = None
        if self.client:
            self.client.stop()
            self.client = None
        self._close_keys()
        self._restore_mpv()
        self._cover()

    # -- main
    def run(self):
        LOG.info("WDBS tune request: %s resolved backend: %s app: %s", self.route.channel, self.route.kind,
                 self.route.app)
        self._cover()
        self.status_fn(self.route)
        self._connect()
        try:
            while True:
                self.sleep(0.05)
                if self.ready_at is not None and self.clock() >= self.ready_at:
                    self.ready_at = None
                    self._signal_ready()
                response = self.input_check()
                if response:
                    if self.player.handle_runtime_command_outcome(response):
                        continue
                    payload = response.payload if isinstance(response.payload, str) else ""
                    if payload.startswith("web_key:"):
                        self._key(payload[8:])
                    elif self.player.is_non_interrupting(response):
                        pass
                    else:
                        LOG.info("global key intercepted: channel command")
                        self.leave("channel change")
                        return response
                self._pump()
                self._title_tick()
        except BaseException:
            self.leave("player stopping")
            raise


def run(player, channel_conf: dict, input_check):
    """field_player entry point for a remote-app station."""
    from fs42.station_manager import StationManager
    from fs42.station_player import update_status_socket

    def status(route, title=None):
        update_status_socket("playing", route.name, route.channel, title or route.name,
                             timestamp=StationManager().server_conf["date_time_format"], content_type="remote_app")

    return RemoteAppChannel(player, channel_conf, input_check, status_fn=status).run()
