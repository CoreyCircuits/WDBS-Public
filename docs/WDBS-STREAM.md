# WDBS Stream: Channel 9's 10-foot interface

> The viewer sees WDBS. Stremio supplies the ecosystem: the account's addons, library, Continue Watching, and the addon
> protocol for catalogs, metadata and streams.

**Scope of this export.** Included: `prodesk/stream/server.py`, the pure cores (`stream_core.py`, `search_core.py`,
`library_core.py`) and the vanilla web UI source. Withheld: fonts, logos and mpv overlay assets (rights unverified),
the app-host unit files and key relay, and any account files. The UI therefore renders with fallback fonts.
Channel routing and failure slates: [CHANNEL-ROUTER.md](CHANNEL-ROUTER.md).

## 1. How it fits together

```
TV host: Channel 9 → streaming client ──LAN──▶ streaming host ◀── private headless screen
                                                   └─ browser kiosk → WDBS Stream UI
                                                   └─ mpv while playing
WDBS Stream UI ──▶ wdbs-stream server (loopback only) ──▶ Stremio account API + addon URLs (catalog/meta/stream)
```

The server (`prodesk/stream/server.py`) serves the UI files and a JSON API, runs mpv, and syncs progress. It listens on
loopback only. All account access is optional and needs a one-time local sign-in; nothing in staging contacts an account.

## 2. The interface

Gen X "Soft Club" mood: urban/transit typography (departure-board header with split-flap CH and clock tiles, line-selector
tabs, numbered rows, a station-sign strip on the hero), cool muted tones, and no animation that costs CPU on a software
encoder. Navigation is d-pad only; body text is sized for 10-foot viewing (about 37 px at 1080p).

Tabs: Home, Library, Search, plus detail pages with seasons/episodes, a source picker, trailers and Continue Watching.
Pure logic for rows, ranking and search lives in the `*_core.py` modules so it is testable without a browser or network.

## 3. Playback and Continue Watching

`POST /api/play` starts mpv full-screen with the chosen URL, resuming from the library's saved point for that exact video
(3 s early; >=90 % watched starts over). While it plays the server polls mpv over IPC; every 60 s and at the end it writes
the library item back (resume point, duration, time watched, `video_id`; finished movies are flagged watched), so every
device's Continue Watching stays in sync. Unknown library fields are preserved. A source that never starts is reported
and the source list opens.

**Trailers.** `/api/meta` returns `trailers: [{title, ytId}]`, normalized by `stream_core.trailers()`; ids must match
`[A-Za-z0-9_-]{11}`, at most 5. The UI posts `kind: "trailer"` and `ytId`; the server ignores any client URL and builds the
video URL itself. Trailers never read or write the library.

## 4. Security

- The UI server listens on loopback only; the TV sees the UI only as pixels.
- The auth file holds only the Stremio **auth key** and user id (mode 600). The password is typed into the local `login`
  command and never stored or logged.
- The key relay (a separate service, not included) must authenticate, allowlist keys and rate-limit.
- Test identifiers never write to an account.

## 5. Resource limits

A runaway Channel 9 stack must be killed inside its own cgroup slice rather than starve the host. The private deployment
caps the browser kiosk, mpv and the streaming host with systemd resource limits and routes audio to a private null sink
so nothing reaches the host's speakers. Those unit files are not included. Never run parallel browser/mpv/compositor
test stacks on a small app host.

## 6. Not yet

Addon subtitles passed to mpv, remembering a subtitle choice across episodes, a trailer chooser, and addon-only catalogs
that require extras.

Tests: `python -m unittest tests.test_stream_core tests.test_stream_trailers tests.test_library_core tests.test_stream_search`.
