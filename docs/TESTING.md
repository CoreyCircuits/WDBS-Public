# Testing

## Canonical command

```bash
python -m unittest discover -s tests -q
```

Run it with a throwaway `HOME` and without `WDBS_*` / `FS42*` environment variables so defaults cannot reach real state:

```bash
env -i PATH="$PATH" HOME="$(mktemp -d)" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

The suite is designed to be offline: synthetic FS42 schema, temporary directories, loopback ephemeral ports, fake
subprocesses and fake client output. It needs no FS42 install, display, hardware, systemd, mpv or external account.
Run suites one at a time; parallel test stacks can exhaust a small host.

## Known expected results in this export

The results below were observed when the export was assembled (Python 3.14.8, Linux). They are not a promise for other interpreters.

- The synthetic FS42-style database is built from `tests/fixtures/wdbs_fs42_min_schema.sql`, a minimal schema authored
  from the columns WDBS reads and writes. It is a subset: real FS42 databases may differ in columns, types, defaults,
  constraints and indexes, and passing tests here does **not** show compatibility with any FS42 revision. The earlier
  production-derived schema file stays withheld (provenance unresolved).
- `tests/test_safety.py::PatcherCompileFirstTests` (4 tests) needs the withheld `wdbs.fs42_patch`; the module-level import is guarded and that class is skipped with an explicit reason (the other 6 tests in the file run). `tests/test_core.py::PathMigrationTests` is skipped because `wdbs.migrate_paths` is withheld. Both skips are `skipIf` on module absence, so with the modules present the tests run unchanged.
- `tests/test_withheld_guards.py` (3 tests, new) checks that the CLI/API report unsupported features cleanly.
- Tests for the withheld key-mapping module and the private key relay were excluded rather than edited.
- Two `tests/test_panels.py::ClockTests` tests are skipped by the test itself ("fonts from wdbs-graphics not present"), because the fonts are withheld. They were also skipped in the baseline run of the unmodified source on the build host.
- Modules that import PySide6, FastAPI or FS42 are SKIPPED in the import check, not imported.

## Adding tests
Place tests beside the behaviour in `tests/test_*.py`, use synthetic data, never contact live services, and follow
`docs/CODING_STANDARDS.md`.
