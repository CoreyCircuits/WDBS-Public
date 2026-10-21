# WDBS Today (Channel 3)

A calm screen for the day: a moving clock, the calendar block you are in and how much of it is left, what is next, a
routine for the time of day, tasks and projects from a task database, local weather, and an optional money summary.

Install and deployment steps (host layout, units, firewall, station registration) are **not** part of this export. The
included pieces are the page (`www/index.html`), the feed aggregator (`prodesk/wdbs_today_feed.py`), an example config,
an example env file, an example station conf and synthetic fixtures.

## Looks

One palette (**drive-in**: tan, cream, rust) with a day and a night version; the page can switch at the local sunrise and
sunset. Fonts are self-hosted in the private deployment; font files are withheld here, so the page falls back to system
fonts. Settings in `today-config.json` under `look`:

- `sky`: `auto` (follow the sun), `day` or `night`
- `motion`: `false` stops the clock animation
- `dim_from` / `dim_to`: hours when the screen dims

## How it fits WDBS

```
Calendar (iCal URLs) ----------+
Task + Projects databases -----+--> feed (timer, every 5 min)
Weather service (no key) ------+        writes www/home.json (atomic)
Optional ledger summary -------+        (money: derived summary only; the token stays in the feed)
www/music ---------------------+
                                           |
                       tiny web server  (page, feed, music)
                                           |
                       TV host: FS42 web station -> fullscreen web view
```

The app host serves, the TV host stays a terminal, and secrets live only in an env file on the app host.

## Files

| Path | What it is |
|---|---|
| `www/index.html` | The channel page (1920x1080 stage) with built-in sample data for preview |
| `prodesk/wdbs_today_feed.py` | Builds `home.json`; `--fixtures DIR` runs offline against saved responses |
| `prodesk/today-config.json` | Look, music, routines, lists, mode rules, property names, weather point (all placeholders) |
| `prodesk/prep-music.sh` | Normalizes tracks for the music bed (ffmpeg loudnorm to -18 LUFS) |
| `env.example` | Calendar iCal addresses and API tokens (placeholders; never commit the real file) |
| `fs42/wdbs_today.json` | Example FS42 web-station conf |
| `tests/fixtures/` | Synthetic offline data for the feed |

## Offline check

```bash
python3 prodesk/wdbs_today_feed.py --fixtures tests/fixtures --dry-run > /dev/null
```

## Using it

**Switch the day.** An all-day calendar event named `Field day`, `Reset day` or `Build day` (or `Mode: field`) picks the
mode; timed events with keywords from the config can also flip it.

| Mode | Extra segment | Hidden |
|---|---|---|
| Build day | none | nothing |
| Field day | checklist | nothing |
| Reset day | bare-minimum list | Tasks, projects, deadline countdowns |

**Time of day changes the rotation.** Mornings lead with the morning routine; evenings add a Tomorrow segment and the
night routine.

**Testing in any browser.** `?at=HH:MM` fakes the clock, `?mode=field` forces a mode, `?sky=day|night` forces the look.
With no feed the page shows sample data and a preview bar.

## Money

An optional Money segment (available, spent today, next 7 days, status) comes from a local ledger-style summary service.
The feed calls `GET /v1/summary` with `LEDGER_TOKEN` and copies only `available`, `spent_today`, `upcoming_7d`, `status`
and `as_of` into `home.json`; the page never sees the token or any transaction. If the service is down the last good
numbers stay up and the bottom line says Money is not updating. Unset `LEDGER_URL` to turn it off.

## When something breaks

Each source fails on its own. The feed keeps the last good copy, records the failure under `health` in `home.json`, and
the bottom line says what is stuck and since when.
