# Capabilities of this export

Status legend: **Included** (code present and covered by offline tests that ran), **Included, needs integration**
(code present; use needs FS42 or services that are not bundled), **Degraded** (present but a withheld asset or module is
missing), **Withheld** (not in this tree).

FieldStation42 compatibility: **unresolved**. No FS42 revision is recorded and none is claimed.

| Capability | Status | Notes |
|---|---|---|
| Control-plane API/CLI (`wdbs/api.py`, `cli.py`) | Included, needs integration | Needs an FS42 root/DB. `player patch-status` and `migrate-paths` depend on the withheld `wdbs.fs42_patch` / `wdbs.migrate_paths`; they now report "not included in this export" (exit 1 / 2) instead of a traceback. |
| FS42 read-only adapter | Included, needs integration | Tests run against a minimal synthetic schema (`tests/fixtures/wdbs_fs42_min_schema.sql`); real-FS42 behaviour is not verified. |
| WDBS state DB, events, recorder, history | Included, needs integration | Recorder/history tests use the same minimal synthetic schema. |
| Policy engine, programming manifest | Included | Pure logic. |
| Guarded replan, `fs42_build` | Included, needs integration | Requires an FS42 checkout; the replan tests use the minimal synthetic schema, not a real FS42 database. |
| Player schedule reload (`player_control`) | Degraded | The reload hook that the code expects is installed by the withheld patch. Without it the code reports an unpatched player and keeps the DB change. |
| Overnight worker, rehearsal sandbox | Included, needs integration | Requires FS42. |
| Weather breaks, prefetch, era-ID | Included, needs integration | Operate on the FS42 DB. |
| Tuning-banner state machine, volume core, era selection | Included | Pure logic, tested. |
| Tuner / volume overlay services (PySide6) | Included, needs integration | Import skipped here: PySide6 not installed. |
| Era bug, side panels, standby slates | Degraded | Logic and tests included; the bundled BGRA/PNG artwork and fonts are withheld, so overlays have no art. |
| Remote command parsing, guard, ratings | Included | Pure logic, tested. |
| Remote HTTP API (`wdbs_remote.py`) | Included, needs integration | Needs FastAPI and an FS42 server to mount into; import skipped here. |
| Phone remote UI and Guide UI (`fs42_overlay/static/`) | **Withheld** | Contains fonts, logos and images with unverified rights. The remote API exists without its web UI. |
| Channel router core, remote-app session loop | Included, needs integration | Core logic tested; the session loop needs FS42 and a streaming host. |
| Catch-up, ad rotation, web-window pre-warm | Included, needs integration | Logic and unit tests included; they must be hooked into an FS42 checkout by a step that is **not** part of this export. |
| FS42 patch application (`wdbs/fs42_patch.py`) | **Withheld** | Embeds upstream FS42 source anchors; licence review pending. No replacement is provided. |
| Physical remote mapping (`wdbs/keys.py`) | **Withheld** | Mixes reusable key-mapping logic with deployment-specific device identifiers and a private PC bridge; isolating the generic part without behaviour change was not attempted. Its tests are excluded. The PC-mode banner (`modebanner.py`) is also excluded. |
| Private PC bridge and tunnel listener | **Withheld** (by decision) | Not part of WDBS Public. |
| Path-migration tool (`wdbs/migrate_paths.py`) | **Withheld** | Contains production data handling and mapping; the design is in `docs/MIGRATION-001-episode-order.md`. |
| WDBS Stream server, cores, web UI | Included, needs integration | Cores tested offline. UI fonts, mpv overlay, logo and Lua script are withheld/unclassified. Key relay and app-host units are not included. |
| WDBS Today feed and page | Included | Feed runs offline against synthetic fixtures (tested). Fonts withheld; page uses fallback fonts. |
| Notion/Sonarr/ratings/stats sync scripts | Included, needs integration | Optional integrations; tests use fakes. They use the placeholder SSH alias `TV_HOST`. |
| Weather-channel feed (`weathercast_feed.py`) | Included, needs integration | Renamed from a personal-name brand. |
| Commercial ingest, music/download helpers | **Withheld** (unclassified) | Not in the approved classification list. |
| Deployment units, provisioning scripts, firewall and pairing steps | **Withheld** | Private. `deploy/examples/` does not exist in the source, so no example units were produced. |
| Integration sandboxes (`tests/integration/`) | **Withheld** | Need a real FS42 install and/or the withheld patch module, or read a live player. |

## Do the advertised features need anything that was withheld?
No withheld file is required to import, test or run the included logic. Withheld items cause only these degradations:
- **WDBS Stream**: `/fonts/*` and `/img/*` are served from the withheld static directory, so the server answers 404 for them
  (it handles this without error); the page falls back to system fonts and shows the alt text instead of the logo.
- **WDBS Today**: `fonts/fonts.css` is absent (404, ignored) and the page uses system fonts; the grain texture is `none`.
- **Era bug, side panels, standby slates, tuning backdrops**: artwork is read from an operator-supplied asset directory that was
  never part of the source tree; without it these overlays have nothing to draw. Their logic and tests are included.
- **FS42 hooks** (catch-up, ad rotation, pre-warm, remote HTTP API, remote-app loop): the code is included but installing it into
  an FS42 player requires the withheld patch step.
