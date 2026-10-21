# WDBS Channel Router and remote-app channels

> A channel is a service. FieldStation42 is one backend behind WDBS; a remote application is another.

**Scope of this export.** The pure routing logic (`fs42_overlay/wdbs_router_core.py`), the session loop
(`fs42_overlay/wdbs_remote_app.py`) and the example registry (`config/remote_apps.json`) are included. The step that
hooks them into an FS42 player (a source patch) is **not** included, and no FS42 revision is claimed compatible.
Host provisioning (streaming-host setup, firewall, pairing) is deliberately left out; hosts in the example registry are
placeholders.

## 1. Channel types

| Type | Backend | Who owns the picture | Examples |
|---|---|---|---|
| `linear` | FieldStation42 (`standard`, `loop`, and FS42's own `web`/`guide` stations) | FS42's mpv or web window | 3, 4, 5, 10, 90, 99 |
| `remote_app` | Moonlight-compatible client on the TV host, streaming from a Sunshine-compatible host | the client window | 9 WDBS STREAM |

Future types (`live_stream`, `camera`, `game`, `system`, ...) are deliberately not built. A remote app is the **App**
presentation primitive; the arbitration "Takeover > App (if tuned) > Channel" is what the router does today.

## 2. Where the router lives

FS42's player main loop is already the channel router: every channel change lands in one place that picks the web,
guide, executable or scheduled-slot path by the station's `network_type`. The least disruptive insertion point is one more
branch there, so there is no second channel list and no second input path.

- **Registry of channels = FS42 station confs.** Channel 9's example conf: `config/stations/wdbs_stream.json`.
- A station is a remote app when it has `"wdbs_service": "remote_app"` and `"wdbs_app": "<key>"`. Its `network_type`
  stays `"executable"` (`"executable_command": "sleep infinity"`), which FS42 knows has no catalog and no schedule, so
  scheduling, overnight, replan, weather and prefetch all ignore it.
- **Registry of apps** (separate from channel identity): `config/remote_apps.json`, read on every tune, so edits need no
  restart.

| Module | Responsibility |
|---|---|
| `wdbs_router_core.py` | pure logic: `resolve()`, registry validation, client argv, log-line classifier, `Session` state machine, slate text, router state |
| `wdbs_remote_app.py` | runs a remote-app channel inside the player loop: cover, client process, keys, slate, retry, leave |

## 3. What happens on `9` + Enter (from a linear channel)

```
tune:9 ──▶ channel change in the player loop
   router state: {mode: REMOTE_APP_MODE, previous_channel: 10, session: connecting}
   1. mpv: playlist-clear + silent black cover  (linear audio and video stop; FS42 keeps running)
   2. status socket: channel 9, content_type "remote_app"
   3. load registry → spawn the streaming client (own process group)
   4. first video packet → session active → release the tuning card
   5. loop every 50 ms: app keys → key injection; any channel command → leave
```

Leaving (any channel command): stop the client (SIGTERM the group, SIGKILL after 1 s), restore mpv, put the black cover
back, return the outcome to FS42, which tunes the target exactly as before. **Linear is wall-clock scheduled**, so time
spent on 9 never affects the schedule.

## 4. Modes and state

A small JSON state file records the mode on every tune and session change:

```json
{"mode": "REMOTE_APP_MODE", "channel": 9, "previous_channel": 10, "name": "WDBS STREAM",
 "backend": "remote_app", "app": "stremio", "session": "active", "reason": null, "at": 1790000000.0}
```

- `mode`: `LINEAR_MODE` (FS42 owns picture and input) or `REMOTE_APP_MODE`.
- `session`: `connecting`, then `active`; or `failed` (with `reason`). Linear channels have `session: null`.

## 5. Input ownership

Every remote goes through the command layer (`parse_command` + `Guard`).

| Control | Command | Owner in REMOTE_APP_MODE |
|---|---|---|
| 0-9 | composed into `tune:N` | **WDBS** (digits are never keys, so they cannot reach the app) |
| CH+ / CH- | `ch_up` / `ch_down` | **WDBS** |
| LAST | `back` | **WDBS** (previous channel) |
| GUIDE, INFO | `guide`, `info` | **WDBS** |
| Arrows, OK | `key:ArrowUp` ... `key:Enter` | **app** |
| BACK in app | `key:Back` | **app** (Escape) |
| Transport | `key:PlayPause` / `Rewind` / `FastForward` | **app** |

Keysyms per app are overridable in the registry (`"keys": {"Back": "BackSpace"}`). Keys pressed while the session is
still connecting are dropped, never queued blind.

## 6. Audio ownership

Exactly one source by construction: entering a remote app replaces FS42's media with a silent cover; the client is the
only thing making sound; leaving kills the client before FS42 plays anything.

## 7. Presentation and failure states

Under the client there is always FS42's fullscreen mpv (black cover or slate), so a closing or crashing client reveals
WDBS, never a desktop.

| Situation | Detected by | Screen | Recovery |
|---|---|---|---|
| host not configured / registry broken | registry validation error | "SERVICE NOT SET UP YET" | fix registry, OK |
| host offline, host service down, unpaired, app missing | client log line or client exit | "SERVICE TEMPORARILY UNAVAILABLE" | OK = retry; any channel command leaves |
| connects but no picture | `connect_timeout_s` | same | same |
| stream drops / client crashes | `Connection terminated` or client exit | same | same |

The unavailable slate is rendered with Pillow when available; the logo asset it normally uses is withheld, so the slate
falls back to text.

## 8. Acceptance tests (spec)

| # | Test | Coverage in this tree |
|---|---|---|
| 1 | linear to 9 shows the app, no desktop | needs a real host (NOT RUN here) |
| 2 | arrows/OK/BACK navigate the app | key path unit-tested with a fake client |
| 3 | digits never reach the app | unit-tested (digits are `tune:N`) |
| 4 | CH+ from 9 goes to the next channel | needs FS42 station order (NOT RUN here) |
| 5 | A to 9 to LAST returns to A | router previous-channel unit-tested |
| 6 | pause, tune away and back keeps state | needs a real host (NOT RUN here) |
| 7 | host offline shows the slate | unit-tested with a fake client (refused, silent, crash) |
| 8 | host back, OK recovers | retry unit-tested |
| 9 | linear aligned after time on 9 | FS42 wall-clock behaviour; scheduler untouched |
| 10 | never double audio | silent cover while on 9; client killed before leaving |

Tests: `python -m unittest tests.test_router_core tests.test_remote_app`.

## 9. Adding another remote application

1. Add an entry to `config/remote_apps.json` (`provider: sunshine`, `host`, `application`, stream settings, optional
   `keys` overrides) and the matching application on the streaming host.
2. Add a station conf: `network_type: executable`, `executable_command: sleep infinity`, `wdbs_service: remote_app`,
   `wdbs_app: <key>`, a free `channel_number`, a `network_name`.
3. Optional: a tagline in `wdbs/tuner_core.TAGLINES`.
4. Restart the player (new station). Nothing in the router is app-specific.
