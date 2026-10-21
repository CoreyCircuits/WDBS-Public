# WDBS / FS42 bug notes (condensed)

Measurements, timestamps and host details from the private log are intentionally omitted. These notes keep the
mechanisms and mitigations because the code references them.

## BUG-001: mpv playback-start stalls (> 10 s)

**Symptom.** Occasional "Timeout waiting for playback to start" for any entry (even a local image), recovering without a
process restart.

**Code path (FS42 `station_player.play_file`).** It calls `mpv.play()`, then polls `mpv.time_pos` every 50 ms. If playback
has not started after `video_seek_timeout` (default 10 s) it returns False, then seeks via `_seek_with_verify()` (up to
another 10 s).

**Evidence pattern.** Stalls hit non-network entries, so it is not one bad file. Memory pressure (swapped-out mpv and
player pages on slow flash storage) and IO pressure (cold reads from a contended spinning disk behind a network share)
both correlate with stalls. A concurrent heavy job (for example a replan rehearsal) can contribute.

**Mitigations.**
- Rehearsal/build jobs run under `nice -n 19 ionice -c 3`.
- `wdbs prefetch` (`wdbs/prefetch.py`): every 30 s warms each not-yet-started plan entry up to 10 minutes ahead (head, tail
  and a window at the planned seek offset), at idle IO priority with a bandwidth cap, with a final touch shortly before air.
  Missing upcoming media emits `prefetch.missing_media`.
- Raise `video_seek_timeout` modestly. Note that every retry costs content (BUG-002).
- Free RAM on the TV host; keep imports off the broadcast disk at airtime; localize or cache cloud-backed media.

## BUG-002: wall-clock catch-up skips protected feature content

**Mechanism (verified in FS42 source at the time).**
1. *Per-entry drift.* In `_play_from_point` each entry's end is `now() + (entry.duration - initial_skip)`, taken after mpv
   start and seek-verify. Start latency is added to every entry and never reconciled with the block timeline, so a block
   with 10-15 entries can overrun by tens of seconds.
2. *Wall-clock rejoin.* The next block (or any retry after FAILED) calls `play_slot(now)`, which is pure wall clock. An
   overrun or outage of N seconds skips N seconds of whatever is next, usually a feature opening. A retry in a break joins
   commercials near their end.
3. *Block-tail gap.* If a block's plan is shorter than the block (rounding), `get_play_point()` finds no entry for the tail,
   MainLoop logs "Player failed to start", and a standby image shows until the boundary.

**Policy implemented in `fs42_overlay/wdbs_catchup.py`.**

| Rule | Behavior |
|---|---|
| Protected feature start | A feature joined <= 60 s late restarts at its planned start. A deeper tune-in stays live. |
| No near-end break joins | A commercial or bump joined with < 10 s left, or past half, is skipped (chains across breaks). |
| Absorb lateness in breaks | While playout is at least a whole commercial or bump behind the block timeline, that entry is skipped. **Features are never shortened or skipped.** |
| Tail hold | When a block's content ends before the block does, hold until the boundary instead of FAILED then standby. |

Weather break entries stay skippable like any break filler; only program content is protected. A consecutive `weather`
group is skipped whole or not at all. `WDBS_CATCHUP=0` disables the policy.

Hooking this policy into the FS42 player required a source patch that is **not** part of this export; the module and its
unit tests (`tests/test_catchup.py`) are.

## BUG-003: legacy overnight appends with the previous night's lineup (fixed in `wdbs.overnight`)

The legacy overnight tool wrote the new template, then called functions that reach the station config through
`StationManager().station_by_name()`. `StationManager` is a borg singleton loaded once per process, before the write, so
the sequence scan and the appended day used the lineup from the start of the run. `wdbs.overnight` calls
`StationManager().load_json_stations()` after writing and checks that the reloaded config shows the new template before
scanning or appending.

## Note: overnight result feedback

The TV host writes a catalog result JSON locally; a read-only share cannot carry it back. `prodesk/wdbs_result_pull.py`
copies it to the app host over SSH only when `generated_at` changes, and a failure never blocks the downstream export.
