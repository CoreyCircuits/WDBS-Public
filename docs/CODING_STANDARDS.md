# Coding standards

Human-readable rules. Project-wide boundaries and safety rules are in `AGENTS.md`; this file does not repeat them.

## Structure and naming
- One responsibility per module. Keep decision logic in pure functions (`*_core.py`, `policy.py`, `tuner_core.py`) and keep
  I/O (sockets, subprocesses, files, databases) in thin shells around them so the logic is testable without a host.
- Names say what a thing is for: `fs42_adapter` reads FS42, `state` owns WDBS state. Do not add a second helper for something an existing module already does.
- Dependencies are explicit: pass paths, clocks, sockets and factories in (the existing classes take `*_fn` / `*_factory`
  arguments for this) instead of reaching for globals. Standard library first; optional packages (Pillow, PySide6,
  FastAPI) must be imported lazily and degrade with a clear message.

## Error handling
- Do not swallow exceptions where failure matters. Catch the narrowest exception, log or return a reason, and let
  safety checks fail closed (for example "only `inactive` or `failed` count as idle").
- Operations that mutate shared state refuse first, explain why, and leave the state untouched when they refuse.

## Comments
Comments explain **why**, never what the syntax does. Write one where the reader could otherwise break an invariant:
- FS42 contracts the code depends on (table semantics, cursor behaviour, what a reload does and does not do);
- scheduling invariants (never split or interrupt the block on air, wrap-around of sequences);
- locking and concurrency (lock order, what holds the write lock, why a check fails closed);
- database safety (read-only opens, backup before write, compare-and-swap, restore on failure);
- subprocess lifecycle (process groups, termination order, timeouts);
- hardware or display constraints (overlay geometry, memory caps, software encoding);
- security boundaries (what a token may reach, why input is allowlisted);
- compatibility limits (assumptions about FS42 that are unverified).

## Docstrings
Public modules, classes and functions that expose an external contract need an accurate docstring covering: purpose,
arguments that are not obvious, side effects and mutations, refusal and error conditions, filesystem or process behaviour,
and compatibility constraints. Keep them current when behaviour changes. A one-line docstring is fine for a pure helper.

## Testing
- Unit tests run offline on synthetic data; no live FS42, accounts, hardware, display or systemd.
- Use temporary directories, ephemeral loopback ports and fake subprocesses. Clean up sockets and servers in `tearDown`.
- A bug fix includes a regression test that fails without the fix.
- Never delete or weaken a test to get green. Mark an unrunnable test SKIPPED with the reason.

## Compatibility
- Record assumptions about FS42 behaviour in the module docstring. Do not claim support for an FS42 revision that has not been verified.
- Schema or file-format changes to WDBS state need a versioned migration in `wdbs/state.py` and a test.

## Filesystem and database behaviour
- Open FS42 databases read-only (`mode=ro`) unless the function is the single documented writer.
- Write files atomically (temp file, then rename). Never overwrite a file another process has mapped (for example an
  image mpv is displaying); replace it by rename or stop the consumer first.
- Back up before any write to a database you do not own, verify afterwards, and restore automatically on failure.
- Use the SQLite backup API to copy a live database.
