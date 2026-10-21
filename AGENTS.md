# AGENTS.md

Canonical rules for any AI coding agent or human contributor working in this repository. Agent-specific execution
guidance is in `.github/copilot-instructions.md`; human-readable style rules are in `docs/CODING_STANDARDS.md`.

## Public / private boundary
- This repository is the **public** WDBS tree. Never add real hostnames, addresses, usernames, SSH aliases, service
  URLs, account or database identifiers, calendar URLs, coordinates, filesystem paths from a real deployment, or
  household/personal content. Use placeholders: `example.invalid`, `TV_HOST`, `APP_HOST`, `/path/to/FieldStation42`,
  `/srv/example-media`.
- Never add fonts, logos, generated graphics, binary overlays or third-party source whose redistribution rights are
  not recorded in `THIRD_PARTY_NOTICES.md`.
- `wdbs/fs42_patch.py` and anything that reproduces FieldStation42 source text is out of scope (license review pending).
  Do not recreate patch definitions in another file.
- Do not change `LICENSE` or add per-file licence text without maintainer approval. Original source is GPL-3.0-or-later; contributions are accepted under the same licence.

## No production access
- Do not run SSH, rsync, deployment, systemd, FS42 player, mpv, Sunshine/Moonlight, or hardware-input commands.
- Do not read or write real credentials, tokens, runtime databases, logs or media catalogs.
- Tests and tools must use temporary directories, synthetic schemas, ephemeral loopback ports and fake subprocesses.
- External integrations (calendar, task database, ledger, Stremio, weather) are optional and must never contact live
  accounts in tests or CI.

## Tests and no-regressions policy
- Run `python -m unittest discover -s tests -q` before and after a change and report the real result.
- Never delete, skip, weaken or rewrite a test to make a run pass. If a test cannot run because a dependency is
  withheld or missing, say so and record it; do not hide it.
- A behaviour change needs a focused reason and a regression test. Keep changes small and reviewable.
- Do not claim FS42 compatibility with any revision: the compatible revision is unresolved.

## Safety-sensitive areas (extra care, extra tests)
- Scheduling and sequence cursors (`wdbs/replan.py`, `wdbs/fs42_build.py`, `wdbs/policy.py`, `wdbs/overnight.py`).
- Database safety: read-only FS42 access (`wdbs/fs42_adapter.py`), the write lock, backup/verify/restore, insert-only
  catalog sync.
- Routing and remote commands (`fs42_overlay/wdbs_router_core.py`, `wdbs_remote_core.py`): the guard, digit/key
  ownership, and leave-on-channel-command semantics.
- Player reload (`wdbs/player_control.py`) and subprocess lifecycles (process groups, SIGTERM then SIGKILL).
- Anything that handles tokens or authentication files.

## Review expectations
- State what was run and what was not. Mark checks PASS / FAIL / SKIPPED / NOT RUN with evidence.
- Explain *why* in comments and docstrings, not what the syntax does.
- Prefer existing abstractions; do not add dependencies or frameworks without a recorded reason.
