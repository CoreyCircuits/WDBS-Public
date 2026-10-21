# Third-party notices (draft, not a license grant)

## FieldStation42 (external dependency, not bundled)
- WDBS integrates with [FieldStation42](https://github.com/shane-mason/FieldStation42), which the upstream project
  identifies as MPL-2.0 licensed. **FieldStation42 source is not included in this tree.**
- Several modules read FS42's SQLite schema and status files, call its APIs and private methods, or are written to be
  hooked into an FS42 checkout (`wdbs/fs42_adapter.py`, `wdbs/fs42_build.py`, `fs42_overlay/*`).
- **Exact compatible FS42 revision: unresolved.** Source: not recorded in the WDBS repository. No compatibility claim is
  made. Required owner input: the deployment's FS42 commit/tag or a reproducible revision record.
- `wdbs/fs42_patch.py` (anchored source patches) is withheld pending focused licence review.
- `tests/fixtures/fs42_schema.sql` (a schema file of unconfirmed provenance) is withheld. The tests use `tests/fixtures/wdbs_fs42_min_schema.sql` instead: a minimal schema independently authored from the interface WDBS uses (see its header). It is not verified against any FS42 revision.

## Other software the code talks to (not bundled)
Stremio and its addons, Sunshine/Moonlight, mpv, Sonarr, a Notion-compatible API, the US National Weather Service API.
Each remains under its own terms; WDBS ships none of them.

## Fonts, logos and graphics
None are included. All fonts, logos, generated graphics and binary overlays were withheld pending verification of
ownership and redistribution rights (see `docs/PUBLIC_RELEASE.md`).
WDBS branding is separately owned and licensed.

## Original WDBS code
Licensed **GPL-3.0-or-later** (`LICENSE` is the unmodified GNU GPL version 3 text). Copyright (C) 2026 Corey Jarrett.
The tree contains no vendored third-party source: no minified or bundled libraries, no CDN references, no copied
third-party code was identified. A few helper functions re-implement documented FS42 rules in WDBS's own words (for example
date-key matching in `wdbs/fs42_build.py` and sequence wrap-around in `wdbs/fs42_adapter.py`); they are behaviour
re-implementations, not copies of FS42 source. WDBS branding is separately owned and licensed. Third-party material keeps
its own terms.
