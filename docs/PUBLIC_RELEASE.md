# Release scope and known limitations

This is the **initial reference release**: readable, tested architecture and logic from a working personal broadcast
system. It is not a turnkey installer and makes no FieldStation42 compatibility claim.

## What the release is
Control plane, policy/replan engine, recorder, presentation logic, router/remote cores, WDBS Stream, WDBS Today, example
configuration and an offline test suite. Original source is GPL-3.0-or-later (`LICENSE`).

## Deliberately not included
| Item | Why | Effect |
|---|---|---|
| FS42 patch tooling (`wdbs/fs42_patch.py`) | embeds upstream FS42 source anchors; MPL review not done | the FS42-side modules cannot be hooked into a player from this tree; `wdbs player patch-status` reports "not included" |
| Production-derived FS42 schema fixture | provenance unconfirmed | tests use an independently authored minimal schema (`tests/fixtures/wdbs_fs42_min_schema.sql`); it is a subset, not verified against real FS42 |
| Fonts, logos, graphics, static web UIs (`fs42_overlay/static/`) | redistribution rights unverified | Stream/Today pages use fallback fonts; Stream header logo is missing (alt text shows); phone-remote and guide web UIs are absent; era bug/panel artwork must be supplied by the operator |
| Physical-remote mapping, PC bridge, key relay, deployment units, provisioning, path-migration tool | private deployment specifics | documented as omitted; none is needed by the included tests |

## FieldStation42
Not bundled. The exact revision WDBS was developed against is **not recorded**, so no version is claimed to work. Treat the
FS42-facing code as a reference for the integration contracts described in `docs/ARCHITECTURE.md`.

## Testing status
`python -m unittest discover -s tests -q` was run offline on Python 3.14.8, and CI runs it on Python 3.12 (green on the initial
commit, as reported by the maintainer). No other interpreter version is claimed. See `docs/TESTING.md`.

## Settled decisions
- The weather channel brand is "WeatherCast"; the example station name is "EXAMPLE SITCOMS".
- Public TV series titles remain in fixtures and docstrings.
- Notion property names (`Scheduled For`, `Rotation Weight`, `Max Per Day`, `Era Override`, `Premiere Year`, lane values) are
  example names. The Today feed reads its property names from `today/prodesk/today-config.json`; `wdbs/policy.py`, `wdbs/era.py`
  and the sync scripts use them as fixed names.
- The `prodesk/` directory name is a historical path name for the app-host role and is kept.
- Licence: GPL-3.0-or-later.

## Possible future work (not required for this release)
Record the FS42 revision and add a verified compatibility statement; licence review and a clean re-implementation of the patch
step; a verified or upstream-attributed schema fixture; cleared fonts/logos/graphics; per-file SPDX headers; pinning CI actions by commit; making Notion property names configurable; renaming `prodesk/` to `apphost/`;
a generalized installer, other platforms and a hardware matrix.
