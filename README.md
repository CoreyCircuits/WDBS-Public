# WDBS

WDBS is a personal broadcast operating system: a control plane and presentation layer for a household,
cable-TV-style channel lineup. It schedules linear programming, routes channels, records what actually aired,
draws on-screen presentation (tuning banners, volume meter, standby slates, era bugs), and integrates optional
web channels and a remote-app channel.

[FieldStation42](https://github.com/shane-mason/FieldStation42) (FS42) is the linear-TV playout engine
underneath. WDBS sits above it. **FS42 is not bundled, and no FS42 revision is recorded or claimed to be
compatible** (see `THIRD_PARTY_NOTICES.md`).

> This is a **reference implementation**, not a turnkey installer. It was extracted from one household's
> setup; hosts, accounts, media and deployment are intentionally left to you.

> **Status: initial reference release.** Original WDBS source is licensed **GPL-3.0-or-later** (see `LICENSE`). Some
> material is deliberately not included (FS42 patch tooling, fonts/logos/graphics, private deployment pieces); see
> `docs/CAPABILITIES.md` and `docs/PUBLIC_RELEASE.md` for exactly what, and what that means for running it.

## What is in this tree

| Area | Contents |
|---|---|
| `wdbs/` | Control plane: CLI/API, FS42 read-only adapter, WDBS state DB, recorder, history, policy engine, guarded replan, overlays/tuner/volume/slate services |
| `fs42_overlay/` | FS42-side modules: remote command core, channel router core, remote-app session loop, catch-up, ad rotation, pre-warm, catalog sync tool. Not installable without a patch step that is **not** part of this release |
| `prodesk/` | Server-side helpers (named for the "app host" role): WDBS Stream (server, pure cores, web UI), Notion/Sonarr sync scripts, feed scripts |
| `today/` | WDBS Today (Channel 3): feed aggregator, page, synthetic fixtures |
| `config/` | Example station, remote-app registry and poster-pin files (placeholders only) |
| `docs/` | Architecture, channel router, Stream, bug notes, coding standards, capability and release documents |
| `tests/` | Offline unit tests on synthetic data |

See `docs/CAPABILITIES.md` for exactly what works, what is withheld and why.

## Limitations of this release
- **FS42 patching is not included.** The FS42-side modules cannot be hooked into a player from this tree; `wdbs player patch-status` and `wdbs migrate-paths` report "not included".
- **No fonts, logos, graphics or static web UIs are included** (rights unverified). Pages fall back to system fonts; overlay artwork must be supplied by the operator.
- **Compatibility is unverified.** No FS42 revision is recorded or claimed, and no Python version is claimed beyond what `docs/TESTING.md` states was actually run.
- Private deployment pieces (units, provisioning, physical-remote mapping, PC bridge) are not part of this repository.

## Host roles (conceptual)

- **TV host**: runs FS42 and the WDBS control plane; plays channels; receives remote commands.
- **App host**: stores/serves media, runs optional automation and WDBS Stream.
- **LAN client**: an optional PC that can act as a remote-app host.

All hostnames, addresses and paths in this tree are placeholders (`TV_HOST`, `app-host.example.invalid`,
`/path/to/FieldStation42`, `/srv/example-media`).

## The control-plane CLI

```bash
wdbs status                                   # normalized state
wdbs verify                                   # read-only invariant/health checks
wdbs plan --start "tomorrow 06:00" --why      # policy-engine lineup with Algorithm Reasons
wdbs replan --channel 10 --from "tomorrow 06:00"            # dry run on a DB copy
wdbs replan --channel 10 --from "tomorrow 06:00" --apply --expect <digest>
```

A replan never rebuilds the FS42 catalog, and `--apply` only runs against a digest from a reviewed dry run.

## Tests

```bash
python -m unittest discover -s tests -q
```

The suite is offline. The tests build a synthetic FS42-style database from a minimal, independently authored schema
(`tests/fixtures/wdbs_fs42_min_schema.sql`, not verified against any FS42 revision); see `docs/TESTING.md` for the current expected results.

## License

Copyright (C) 2026 Corey Jarrett. WDBS-authored source is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See `LICENSE`.
Third-party software it talks to (FieldStation42, Stremio, ...) keeps its own terms (`THIRD_PARTY_NOTICES.md`); WDBS
branding is licensed separately.

## Docs

- `docs/ARCHITECTURE.md`: control plane design and safety rules
- `docs/CHANNEL-ROUTER.md`: channel types, routing and remote-app channels
- `docs/WDBS-STREAM.md`: the Channel 9 streaming interface
- `docs/BUGS.md`: engineering notes on playout stalls and catch-up
- `docs/CODING_STANDARDS.md`, `AGENTS.md`: contribution and AI-agent rules
- `docs/PUBLIC_RELEASE.md`: release scope and known limitations
