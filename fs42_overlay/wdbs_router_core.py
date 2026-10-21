"""WDBS Channel Router + remote-app session logic - pure, no FieldStation42 imports (installed as
fs42/wdbs_router_core.py by the remote-app patch; fs42/wdbs_remote_app.py wires it to FS42).
Docs: docs/CHANNEL-ROUTER.md

A channel is a service. FS42 station confs stay the one channel registry (Guide v2, CH+/CH-, the tuner and
Remote v2 all read them); a station with "wdbs_service": "remote_app" is routed to a remote application instead
of FS42's own players. Everything else is FS42's (linear, web, guide), unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

LINEAR_MODE = "LINEAR_MODE"          # FS42 owns presentation and input (linear, web and guide stations)
REMOTE_APP_MODE = "REMOTE_APP_MODE"  # a remote application owns the picture; WDBS keeps the global keys

PROVIDERS = {"sunshine", "command"}  # command = any local client argv (tests, future non-Moonlight apps)

# Remote key names (Remote v2 "key:<name>", later a physical remote) -> X keysym sent to the app client.
# Everything that is not a key - digits (the remote sends tune:N), CH+/CH-, GUIDE, BACK (= last channel),
# INFO, EXIT, POWER - is a WDBS command and never reaches the app. An app may override any mapping.
DEFAULT_APP_KEYS = {
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "Enter": "Return", "Back": "Escape", "PlayPause": "space",
    "FastForward": "Right", "Rewind": "Left", "PageUp": "Prior", "PageDown": "Next",
    # USB remote keyboard (2026-10-05): typing into the app. Space travels as underscore because "space" is
    # PlayPause (Stream search: delete a letter); Stream reads "_" as a typed space.
    "Space": "underscore", "Backspace": "BackSpace", "Search": "slash",
    "PrevTrack": "XF86AudioPrev", "NextTrack": "XF86AudioNext",   # Stream player: previous / next episode
    **{c: c for c in "abcdefghijklmnopqrstuvwxyz"},
}
WDBS_GLOBAL = ("digits", "tune", "ch_up", "ch_down", "guide", "back", "info", "exit", "power")

# Moonlight log lines that decide a session (moonlight-qt 6.1 strings, checked in the binary)
_READY = ("Received first video packet",)
_FAILED = ("Starting ", "Failed to connect to", "Connection terminated", "Failed to find application",
           "Failed to find ANY working", "Failed pairing", "is not paired", "Pairing required",
           "Failed to establish ENet connection", "Failed to initialize sockets")
_FAILED_START = " failed"   # "Starting %1 failed: Error %2" (plain "Starting video stream..." is progress)
_INTERESTING = ("Video stream is", "Received first audio packet", "Resuming", "Starting ")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteApp:
    key: str
    name: str
    provider: str
    host: str = ""
    application: str = ""
    persistent: bool = True
    resolution: str = "1920x1080"
    fps: int = 60
    bitrate_kbps: int = 15000
    codec: str = "H.264"
    connect_timeout_s: float = 25.0
    ready_after_s: float | None = None   # command provider: running this long counts as the picture
    command: tuple = ()
    keys: dict = field(default_factory=dict)
    key_relay: str = ""                  # http://host:port/key: keys go to the app host's WDBS key relay, not the client

    def keysym(self, key: str) -> str | None:
        return {**DEFAULT_APP_KEYS, **self.keys}.get(key)


@dataclass(frozen=True)
class Route:
    channel: int | None
    name: str
    kind: str              # "linear" (every FS42 type: standard/loop/web/guide) | "remote_app"
    network_type: str
    app: str | None = None

    @property
    def mode(self) -> str:
        return REMOTE_APP_MODE if self.kind == "remote_app" else LINEAR_MODE

    @property
    def backend(self) -> str:
        return "remote_app" if self.kind == "remote_app" else f"fs42/{self.network_type}"


def resolve(station: dict) -> Route:
    """The route for an FS42 station conf."""
    ntype = station.get("network_type") or "standard"
    name = station.get("network_name") or f"CHANNEL {station.get('channel_number')}"
    if station.get("wdbs_service") == "remote_app":
        return Route(station.get("channel_number"), name, "remote_app", ntype, station.get("wdbs_app"))
    return Route(station.get("channel_number"), name, "linear", ntype)


def load_apps(data: dict) -> dict[str, RemoteApp]:
    """Validate the remote-app registry ({"remote_apps": {key: {...}}}). Raises ConfigError."""
    if not isinstance(data, dict) or not isinstance(data.get("remote_apps"), dict):
        raise ConfigError("registry needs a remote_apps object")
    out = {}
    for key, a in data["remote_apps"].items():
        if not isinstance(a, dict):
            raise ConfigError(f"{key}: not an object")
        provider = a.get("provider")
        if provider not in PROVIDERS:
            raise ConfigError(f"{key}: provider must be one of {sorted(PROVIDERS)}")
        if provider == "command" and not (isinstance(a.get("command"), list) and a["command"]):
            raise ConfigError(f"{key}: provider command needs a command list")
        res = str(a.get("resolution", "1920x1080"))
        w, _, h = res.partition("x")
        if not (w.isdigit() and h.isdigit()):
            raise ConfigError(f"{key}: bad resolution {res!r}")
        relay = str(a.get("key_relay") or "")
        if relay and not relay.startswith("http://"):
            raise ConfigError(f"{key}: key_relay must be an http:// URL")
        keys = a.get("keys") or {}
        if not isinstance(keys, dict) or not all(isinstance(v, str) and v for v in keys.values()):
            raise ConfigError(f"{key}: keys must map remote key names to X keysym names")
        out[key] = RemoteApp(
            key=key, name=str(a.get("name") or key), provider=provider, host=str(a.get("host") or ""),
            application=str(a.get("application") or ""), persistent=bool(a.get("persistent", True)),
            resolution=res, fps=int(a.get("fps", 60)), bitrate_kbps=int(a.get("bitrate_kbps", 15000)),
            codec=str(a.get("codec", "H.264")), connect_timeout_s=float(a.get("connect_timeout_s", 25.0)),
            ready_after_s=float(a.get("ready_after_s", 1.5)) if provider == "command" else None,
            command=tuple(str(x) for x in a.get("command") or ()), keys=dict(keys), key_relay=relay)
    return out


def load_app(path: Path, key: str | None) -> RemoteApp:
    """One app from the registry file, or ConfigError saying why this channel can't start."""
    if not key:
        raise ConfigError("channel has no wdbs_app")
    try:
        apps = load_apps(json.loads(Path(path).read_text()))
    except OSError as e:
        raise ConfigError(f"registry unreadable: {e}")
    except ValueError as e:
        raise ConfigError(f"registry invalid: {e}")
    if key not in apps:
        raise ConfigError(f"app {key!r} is not in the registry")
    app = apps[key]
    if app.provider == "sunshine" and not (app.host and app.application):
        raise ConfigError(f"app {key!r} has no Sunshine host/application yet")
    return app


def client_argv(app: RemoteApp, moonlight: str) -> list[str]:
    """The client process for a session. Sunshine: Moonlight's CLI stream mode, fullscreen, never quitting the
    host app when the stream ends (that is what keeps Stremio's state across channel changes)."""
    if app.provider == "command":
        return list(app.command)
    return [moonlight, "stream", app.host, app.application,
            "--resolution", app.resolution, "--fps", str(app.fps), "--bitrate", str(app.bitrate_kbps),
            "--video-codec", app.codec, "--display-mode", "fullscreen"]


def classify_line(line: str) -> str | None:
    """'ready' | 'failed' | 'info' (worth a log line) | None (noise) for one client output line."""
    if any(s in line for s in _READY):
        return "ready"
    for s in _FAILED:
        if s in line:
            if s == "Starting " and _FAILED_START not in line:
                continue
            return "failed"
    if any(s in line for s in _INTERESTING):
        return "info"
    return None


class Session:
    """idle -> connecting -> active; connecting/active -> failed. Times are monotonic seconds."""

    def __init__(self, timeout_s: float = 25.0, ready_after_s: float | None = None):
        self.timeout_s, self.ready_after_s = timeout_s, ready_after_s
        self.state, self.since, self.reason = "idle", 0.0, None

    def start(self, now: float) -> None:
        self.state, self.since, self.reason = "connecting", now, None

    def fail(self, now: float, reason: str) -> None:
        self.state, self.since, self.reason = "failed", now, reason

    def line(self, now: float, text: str) -> str | None:
        kind = classify_line(text)
        if kind == "ready" and self.state == "connecting":
            self.state, self.since = "active", now
        elif kind == "failed" and self.state in ("connecting", "active"):
            self.fail(now, text.split(" - ", 1)[-1].strip()[:160])
        return kind

    def exited(self, now: float, code: int | None) -> None:
        if self.state == "connecting":
            self.fail(now, f"client exited before the picture (code {code})")
        elif self.state == "active":
            self.fail(now, f"connection lost (client exited, code {code})")

    def tick(self, now: float) -> None:
        if self.state == "connecting" and self.ready_after_s is not None and now - self.since >= self.ready_after_s:
            self.state, self.since = "active", now
        elif self.state == "connecting" and now - self.since >= self.timeout_s:
            self.fail(now, f"no picture after {self.timeout_s:.0f} s")


def failure_lines(route: Route, reason: str | None) -> dict:
    """What the WDBS unavailable slate says. The reason is for the log; the screen stays a cable box."""
    not_set_up = bool(reason) and ("registry" in reason or "no Sunshine host" in reason or "not in the" in reason)
    return {"channel": f"CHANNEL {route.channel}", "name": route.name,
            "headline": "SERVICE NOT SET UP YET" if not_set_up else "SERVICE TEMPORARILY UNAVAILABLE",
            "hints": [("OK", "RETRY"), ("BACK", "PREVIOUS CHANNEL")]}


def router_state(route: Route, previous: int | None, session: str | None = None, reason: str | None = None,
                 at: float | None = None) -> dict:
    """~/.local/state/wdbs/router.json: which backend owns the screen and input right now."""
    return {"mode": route.mode, "channel": route.channel, "previous_channel": previous, "name": route.name,
            "backend": route.backend, "app": route.app, "session": session, "reason": reason, "at": at}


def ready_url(channel) -> str:
    """The 'page loaded' signal wdbs-tuner waits for on a remote-app tune (it releases the tuning card)."""
    return f"wdbs-app:{channel}"
