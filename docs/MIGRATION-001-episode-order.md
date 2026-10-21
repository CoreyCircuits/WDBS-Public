# MIGRATION-001: episode-order fix (generic description)

The original migration tool (`wdbs/migrate_paths.py`), its path-mapping table and the execution log are **not**
included in this export: they contain production library data. This document keeps the design.

## Problem
FS42 orders a sequence by sorting file **paths**. A series whose files mix naming styles airs out of order:

| Cause | Example |
|---|---|
| Space sorts before `.` | `Show - S01E21 ...` sorts before `Show.S01E01 ...` |
| Different numbering style | `1x07` versus `S01E07` |
| Episode filed under the wrong season folder | `Season 5/... 4x24 ...` |

## Approach
Rename files to one scheme (the downloader's configured scheme), then update the FS42 DB **in place by catalog id** in one
transaction so IDs never change:

| Table | Change |
|---|---|
| `catalog_entries` | `path`, `realpath`, `title` updated in place by id; no inserts or deletes |
| `sequence_entries` | `fpath` updated; `sequence_index` renumbered to the new sorted order |
| `named_sequence.current_index` | remapped so the **same next episode** is scheduled |
| `liquid_blocks.plan_json` | old paths replaced in **current and future** blocks only; history untouched |
| `file_meta` / `chapter_points` / `break_points` | path keys updated where they match |

Never uses the stock catalog rebuild or the FS42 schedule reset.

## Procedure
0. Prerequisites: the player can reload its schedule, memory headroom, `wdbs verify` is OK.
1. Choose a window when neither the block on air nor the next block is an affected series.
2. Re-run the dry run against a fresh preview; the mapping digest must still match.
3. Back up the FS42 DB.
4. Run the rename in the downloader for exactly the previewed files.
5. Wait until every new path exists on the TV host and old ones are gone.
6. Apply with `--files-renamed --expect <digest>` (same transaction as the dry run, verified afterwards, backup restored on
   any failed check).
7. Reload the player and run `wdbs verify`.

## Risks and rollback
- Between the rename and the apply an old path does not exist; if the player reaches an affected series then it stalls.
- Before the rename: nothing has changed. After it: forward-fix only, because the original release-group names cannot be restored.
- The insert-only catalog sync would ingest duplicates under new ids if files are renamed without this migration.
- Future downloader quality upgrades rename files the same way; add a recurring check for catalog paths missing on disk.
