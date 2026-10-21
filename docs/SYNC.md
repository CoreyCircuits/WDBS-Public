# Private/public sync check (pilot)

`sync/sync_check.py` is a read-only drift check for the components listed in `sync/manifest.json`. It compares three
SHA-256 fingerprints per component: the private checkout's committed file, this repository's committed file, and the
fingerprint recorded at the last review. It prints states and short hashes only, never source text.

States: `IDENTICAL`, `PRIVATE_AHEAD`, `PUBLIC_AHEAD`, `DIVERGED`, `MANIFEST_STALE` (both sides equal but the recorded
fingerprint is old), `MISSING`. `SANITIZED` / `PUBLIC_ONLY` entries are listed but never compared. Exit code 0 = nothing to
review, 1 = review needed, 2 = manifest/environment error.

## Maintainer workflow
1. After changing a shared component in either repository (and committing there), run:
   `python3 sync/sync_check.py --private /path/to/private/WDBS`
2. For every component that needs review, look at the difference yourself in the private checkout (the tool will not show
   it), decide what is public-safe, apply the change to the public repo by hand, and run the tests listed for that component.
3. Commit in the public repo, then update `reviewed_public_commit` and `reviewed_fingerprint` in `sync/manifest.json`
   (the fingerprint is the sha256 of the committed file: `git show HEAD:<path> | sha256sum`).
4. Optional: `--record /path/outside/both/repos/state.json` keeps a private-side log of the last run.

## Rules enforced by the tool
Only components listed in the manifest **and already tracked in this public repository** can be compared, so a private-only
file cannot enter the flow. `PRIVATE_ONLY` entries, absolute paths and `..` are rejected. Never add private-only paths, hosts
or history to `sync/manifest.json`.
