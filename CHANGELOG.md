# Changelog

This public changelog is condensed. The private development log (deployment dates, host measurements,
owner decisions) is not published. Items marked *withheld* exist in the private project but are not in this tree.

## Unreleased (public candidate)

### Included
- Control plane: `wdbs.api.ControlPlane`, CLI, FS42 read-only adapter, WDBS-owned SQLite state with migrations,
  event bus, recorder, playout history and metrics.
- Programming: explainable policy engine (hard gates, relaxation ladder, scored components), guarded future
  replan (rehearse on a DB copy, digest-pinned apply, backup, invariant verification, automatic restore),
  overnight worker and rehearsal sandbox, weather-break insertion, era-ID rotation.
- Presentation: tuning-banner state machine, volume core, era bug and side panels, standby slates, and the
  Channel/App/Overlay/Takeover/Scene contracts.
- FS42-side logic: remote command parsing and guard, channel router core, remote-app session loop,
  wall-clock catch-up, ad rotation, web-window pre-warm, insert-only catalog sync tool.
- WDBS Stream (Channel 9): server, pure addon/search/library/stream-ranking cores, vanilla web UI.
- WDBS Today (Channel 3): feed aggregator (calendar, task database, weather, optional ledger summary) and page.
- Synthetic offline tests.

### Withheld (see `docs/CAPABILITIES.md`)
- `wdbs/fs42_patch.py` (embeds upstream FS42 source anchors; license review pending).
- Physical-remote device mapping and the private PC bridge.
- Fonts, logos, generated graphics and the FS42 static guide/remote web UIs (asset rights unverified).
- Deployment units, host provisioning, tunnel and firewall configuration.

### Documentation changes
- Private runbooks, measurements, hostnames and household details removed; paths and hosts are placeholders.
- The weather channel's brand was renamed from a personal-name brand to **WeatherCast** in this export.
