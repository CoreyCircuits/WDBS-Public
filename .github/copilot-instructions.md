# Agent execution guidance

Follow `AGENTS.md` first; this file only adds how to work.

1. **Inspect local evidence before editing.** Read the module, its tests and the relevant section of
   `docs/ARCHITECTURE.md`. Do not rely on memory of how FieldStation42 behaves; the modules document the contracts they assume.
2. **Keep pull requests narrow.** One behaviour or one fix per PR. No drive-by reformatting or Ruff-wide cleanups.
3. **Preserve architecture and behaviour.** Pure `*_core.py` modules stay free of I/O. `fs42_adapter` stays the only
   module that knows the FS42 schema. Replan keeps its order: plan, rehearse on a copy, apply with lock and backup, verify, restore.
4. **Add focused tests** beside the code you change, using synthetic fixtures, temp dirs and fake subprocesses.
5. **Report actual results.** Quote the command and its outcome. A check you did not run is NOT RUN, not PASS.
6. **Stop before production or licensing-sensitive work.** Do not contact hosts or services, apply FS42 patches, add
   assets, or touch `LICENSE`. Ask the maintainer instead.
7. **Placeholders only.** If you need a host, path or identifier in an example, use the placeholders in `AGENTS.md`.
