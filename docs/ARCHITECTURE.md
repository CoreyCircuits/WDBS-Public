# WDBS Broadcast OS: Architecture

> AI plans; deterministic code validates and executes; the owner remains final programmer.

WDBS is a broadcast operating system with five presentation primitives: **Channels, Apps, Overlays, Takeovers,
Scenes**. FieldStation42 (FS42) is the linear-TV playout subsystem underneath WDBS, not the system-wide control
plane. FS42 is an external dependency; it is not bundled and no compatible revision is recorded (see
`THIRD_PARTY_NOTICES.md`).

---

## 1. Runtime model

```
 programming manifest (JSON) ──▶ TV host: overnight worker ──▶ FS42 catalog (insert-only) + schedule append
                                         │
 field player (FS42) ── reads the schedule into memory at start
```

### Roles
| Role | Responsibility |
|---|---|
| App host | Optional: media storage, automation, WDBS Stream, feeds |
| TV host | FS42, the live player, the WDBS control plane |

### FS42 facts that constrain WDBS
These describe the FS42 behaviour WDBS was written against. They are not a compatibility claim for any FS42 revision.
* **DB** `runtime/fs42_fluid.db` (SQLite). Tables used: `catalog_entries`, `liquid_blocks` (schedule;
  `content_json` = catalog id, `plan_json` = playable segments with absolute paths), `named_sequence` +
  `sequence_entries` (episode order), `sequence_group_state`, `file_meta`, `break_points`, `chapter_points`.
* **Every FS42 IO class reads `StationManager().server_conf["db_path"]` at construction**, so the DB can be
  redirected per process. WDBS uses this to rehearse replans on a copy.
* **Sequence cursor semantics**: `named_sequence.current_index` is the index of the *next episode to schedule* in the
  episode list **sorted by file path**. Scheduling returns `episodes[current_index]` and commits `current_index + 1`;
  past `end_index` it wraps to `start_index`.
* **Schedule building**: `LiquidSchedule._fluid(start, end)` walks hour slots (date_overrides > week_overrides >
  weekday template), makes blocks rounded up to `schedule_increment`, builds plans, increments catalog `count`, and
  inserts blocks.
* **FS42's stock schedule reset is unsafe for WDBS**: it rewinds every sequence and rewrites the catalog. WDBS never
  calls it.
* **The live player keeps the schedule in memory.** DB edits (overnight appends, replans) are invisible to a running
  player until it reloads. The private deployment used a reload hook installed by a patch; that patch is **not** part of
  this export, so `wdbs.player_control` will report an unpatched player (see `docs/CAPABILITIES.md`).
* **Status**: `runtime/play_status.socket` is a plain JSON file rewritten each time a plan segment starts.

### Content findings (surfaced by `wdbs verify`)
* Because sequences sort by path, mixed file-naming styles for one series (`Show - S01E21 ...` versus
  `Show.S01E01 ...`) air out of order. Fix at the source with consistent naming; renames change FS42 absolute paths, so
  they must be a planned migration (`docs/MIGRATION-001-episode-order.md`).
* Very small libraries repeat within a day.

---

## 2. Control Plane

Separate package. Runs under the FS42 virtualenv interpreter, standard library only. **No FS42 core file is modified by
the control plane itself.**

```
 wdbs CLI ─┐
 other UIs ├─▶ wdbs.api.ControlPlane ──▶ programming (manifest + overrides + facts)
           │          │   │   │                 └─▶ policy (pure engine, Algorithm Reasons)
           │          │   │   └─▶ replan (plan → rehearse on copy → apply w/ backup → verify → restore)
           │          │   │                    └─▶ fs42_build (child process: LiquidSchedule._fluid)
           │          │   └─▶ fs42_adapter (READ-ONLY FS42 schema/status/stations)
           │          └─▶ state (WDBS SQLite)  ◀── recorder (user service, 5 s poll)
           │                    └─ events (EventBus: durable log + subscribers)
 presentation/: Channel · App · Overlay · Takeover · Scene contracts, FS42Channel, Registry
```

| Module | Responsibility | Writes |
|---|---|---|
| `config` | paths from env (`WDBS_FS42_ROOT`, `WDBS_STATE_DB`, `WDBS_MANIFEST`) | nothing |
| `fs42_adapter` | only module that knows FS42 schema/status format; opens DB `mode=ro` | nothing |
| `state` | WDBS state DB, versioned migrations | WDBS DB |
| `recorder` | status socket → segments → airings; live-state snapshot; events | WDBS DB |
| `history` | Last Aired, Airings 7d/30d, Unique Episodes, Unplayed, Last/Next Episode, Cooldown Until | nothing |
| `programming` | manifest loading, local overrides, per-program facts | nothing |
| `policy` | hard gates, relaxation ladder, soft scoring, reasons | nothing |
| `replan` | plan, rehearse, apply, verify, restore | FS42 DB (guarded) |
| `fs42_build` | child-process wrapper around FS42's own builder | FS42 DB (the one it is pinned to) |
| `api` | `ControlPlane` facade, JSON-able dicts | via replan/state only |
| `cli` | formatting only | nothing |
| `events`, `presentation` | extension points | WDBS DB |

### 2.1 Normalized state
`wdbs status` / `ControlPlane.status()` answers: player online/offline (process scan + recorder heartbeat), current
channel, on-air segment and episode, scheduled block and whether player output matches the DB schedule, next block,
schedule horizon, catalog size, active rotation count, manifest freshness, warnings.

### 2.2 Playout history (what actually aired)
* `segments`: every status write (one per plan segment), de-duplicated.
* `airings`: one row per episode on air: channel, file, catalog id, tag, season/episode, matched FS42 block
  (`schedule_match`=0 flags a player running an older in-memory schedule), start, last seen, end, end reason, playhead
  position, `completed` (>=90%).
* An airing **counts** toward metrics at >=50% played (`history.QUALIFY_FRACTION`).
* Tuned in mid-episode: start is estimated as `segment start - offset`, clamped to block start.
* No backfill from past schedule blocks: that would be *scheduled*, not *aired*.

### 2.3 Replan future (`wdbs replan --channel 10 --from "tomorrow 06:00"`)
1. **Plan (read-only)**: effective cutoff = max(request, now), moved to the end of the block on air and to the end of
   any block straddling the cutoff, so it never interrupts and never splits. Refuses: non-standard stations, child
   sequences, overlapping schedule, cutoff at/after horizon, spans > 14 days, and **any sequence whose cursor disagrees
   with the schedule's last episode**.
2. For each sequence in the deleted range: rewind the cursor to the index of the *first deleted episode* (path-sorted,
   as FS42 does). Give back one play count per deleted block.
3. **Lineup**: `template` = station config as-is; `engine` = WDBS policy engine, injected **in memory** as
   `date_overrides` for the rebuild only (config file untouched). A started hour is locked to the program in it.
4. **Dry run (default)**: copy the DB via the SQLite backup API, run the steps on the copy, report, discard.
5. **Build**: `python -m wdbs.fs42_build` child process, cwd = FS42 root, DB pinned via `server_conf["db_path"]`.
6. **Verify** (all must pass): historical/current blocks byte-identical; only planned blocks deleted; new blocks start
   exactly at cutoff; contiguous; horizon preserved; catalog ids/paths/durations/tags hash identical; sequence
   definitions identical; auxiliary tables identical; no orphan catalog ids; every cursor consistent with the schedule;
   episode order continues across the cutoff; new media exist; counts non-negative.
7. **Apply** requires `--apply --expect <digest>` from a reviewed dry run, refuses while the overnight job runs, takes a
   WDBS write lock, backs up the DB, runs, verifies, and **restores the backup automatically** on any failure.
8. Records a `replan_runs` row and a `replan.applied|failed` event.

**Player caveat:** an applied replan airs after the player reloads its schedule (restart or next schedule panic).

### 2.4 Programming policy engine
Hard gates (non-relaxable): in rotation (manifest and no local deactivation), media available, catalog ready, FS42
sequence exists, Max Per Day, slot lock. Relaxation ladder, used only when a slot would otherwise be empty and always
stated in the reason: `daypart`, then `daypart_capped`, then `repeat_gap`/`cooldown`, then `back_to_back`. Score = sum of
named components (balance, recency, unplayed inventory, airings exposure, rotation weight, ratings, manual boost,
small-library exhaustion penalty). Ties resolve by stable hash. Every slot yields an **Algorithm Reason**. Constants
are at the top of `policy.py`.

### 2.5 API surface (`wdbs.api.ControlPlane`)
`status()`, `player()`, `now(ch)`, `next(ch, n)`, `schedule(ch, start, hours)`, `catalog(ch)`, `history(ch, tag)`,
`verify(ch)`, `plan_lineup(ch, start, end)`, `replan(...)`, `set_show_active(...)`, `events(kind)`. All return JSON-able
dicts; the CLI exposes each with `--json`. No network transport is built.

### 2.6 Extension points
`wdbs.presentation`: `Channel`, `App`, `Overlay`, `Takeover`, `Scene` protocols, `FS42Channel` adapter, `Registry`.
`wdbs.events.EventBus`: durable log in the state DB plus in-process subscribers; external consumers poll `since(last_id)`.

---

## 3. Services layered on the control plane

These are summarized; the code carries the detailed contracts in docstrings.

* **Overnight worker** (`wdbs.overnight`): appends schedule days using the policy engine; reuses insert-only catalog
  sync and readiness checks; backs up before appending and restores if new verify errors appear; reloads the
  `StationManager` after writing the template so the scan and append see the new lineup.
* **Weather breaks** (`wdbs.weather_break`): swaps a run of consecutive commercial entries in one break of a *future*
  block for one timed web entry of exactly the same total length. Block start/end, features and total play length never
  change. Rehearse on a copy, refuse while the overnight job runs, take the write lock, compare-and-swap in one
  transaction, verify, restore on failure. Hours that already have a web entry are skipped, so reruns are no-ops.
* **Era presentation** (`wdbs.era`, `wdbs.bug`, `wdbs.panels`, `wdbs.era_id`): an era (70s/80s/90s/00s) from an
  override or the premiere decade selects the corner bug, 4:3 side panels and station-ID bumps. Graphic assets are
  **not** included in this export.
* **Tuning banner** (`wdbs.tuner_core`, `wdbs.tuner`): pure state machine for the banner that holds until the new channel
  is actually playing; the PySide6 service is a thin shell around it.
* **Standby slates** (`wdbs.slate`): cover start stalls and mid-program freezes instead of exposing a desktop.
* **Prefetch** (`wdbs.prefetch`): warms upcoming media into caches ahead of air; rate-limited and idle priority.
* **Remote commands** (`fs42_overlay/wdbs_remote_core.py`): every remote goes through `parse_command()` and a `Guard`
  (double-tap suppression, one channel change at a time, previous channel for BACK).
* **Channel router** (`fs42_overlay/wdbs_router_core.py`, `docs/CHANNEL-ROUTER.md`): a channel is a service; a remote
  application is a backend like FS42.
* **Wall-clock catch-up** (`fs42_overlay/wdbs_catchup.py`): absorbs lateness in breaks, never in features (`docs/BUGS.md`).
* **Ad rotation** and **pre-warm** (`fs42_overlay/`): commercial rotation fairness and web-window pre-warm.

The FS42-side modules assume an integration step that places them inside an FS42 checkout and hooks them in. That
step (patching) is withheld; see `docs/CAPABILITIES.md`.

## 4. Operations

```bash
wdbs status | now | next -n 5 | schedule 10 --start "tomorrow 06:00" --hours 12
wdbs catalog | channel 10 | history [tag] | verify | events
wdbs plan --start "tomorrow 06:00" --hours 18 --why        # read-only lineup preview
wdbs replan --channel 10 --from "tomorrow 06:00" [--lineup engine]           # dry run
wdbs replan --channel 10 --from "tomorrow 06:00" --apply --expect <digest>
wdbs deactivate-show SHOW_TAG --note "rest it"
wdbs overnight-rehearse --now "tomorrow 04:30" [--lineup legacy] [--deactivate TAG]
```

Rollback of an applied replan: restore the printed `.bak` with the SQLite backup API while the player is stopped.

## 5. Known issues
See [BUGS.md](BUGS.md).

## 6. Safety rules (enforced in code where possible)
Never delete media. Never rebuild the FS42 catalog. Never renumber catalog ids. Never call FS42's stock schedule reset.
No new network ports from the control plane. Never touch credentials or print secrets. Never wipe DBs or configs.
Never stop or restart the live player without approval. Back up before any FS42 write, dry-run first, verify after.
