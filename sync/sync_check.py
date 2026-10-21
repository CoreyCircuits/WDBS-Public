#!/usr/bin/env python3
"""Read-only drift check between this public repo and a private WDBS checkout, for approved SHARED components only.

What it does: for each SHARED component listed in sync/manifest.json it compares three SHA-256 fingerprints: the
private checkout's committed content (git HEAD), this repo's committed content, and the fingerprint recorded when the
component was last reviewed. It reports a state per component and exits non-zero when anything needs human review.

What it never does:
  * print source text or diffs (only states, short hashes and a count of differing lines);
  * read any private path that is not listed in the manifest AND tracked in this public repo (so a private-only file
    cannot enter the comparison, whatever the manifest says);
  * write to either repository (the optional --record state file lives outside both);
  * touch hosts, services or the network.

The manifest is public, so it may only hold public-safe data; PRIVATE_ONLY entries are rejected on purpose.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "sync" / "manifest.json"
ALLOWED_CLASSES = {"SHARED", "SANITIZED", "PUBLIC_ONLY"}   # PRIVATE_ONLY must never be named in a public file
NEEDS_REVIEW = {"PRIVATE_AHEAD", "PUBLIC_AHEAD", "DIVERGED", "MANIFEST_STALE", "MISSING"}


class SyncError(Exception):
    """Manifest or environment problem; the comparison must not proceed."""


def fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> bytes | None:
    """Run a read-only git command; None on failure. --no-optional-locks keeps git from refreshing the index."""
    r = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def load_manifest(path: Path, public_repo: Path) -> list[dict]:
    """Validate the manifest. Every component path must be a plain relative path already tracked in the public repo."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SyncError(f"cannot read manifest: {e}")
    tracked = set((_git(public_repo, "ls-files") or b"").decode().split("\n"))
    out = []
    for c in doc.get("components", []):
        p, cls = c.get("path", ""), c.get("classification")
        pp = PurePosixPath(p)
        if cls not in ALLOWED_CLASSES:
            raise SyncError(f"{p or '<empty>'}: classification {cls!r} is not allowed in a public manifest")
        if not p or pp.is_absolute() or ".." in pp.parts or str(pp) != p:
            raise SyncError(f"{p!r}: path must be a normalized relative path")
        if p not in tracked:   # the guard that keeps private-only files out of the flow
            raise SyncError(f"{p}: not tracked in the public repository, refusing to compare")
        out.append(c)
    return out


def compare(component: dict, private_repo: Path, public_repo: Path) -> dict:
    """Return the state of one SHARED component. Only hashes and a differing-line count leave this function."""
    p = component["path"]
    priv = _git(private_repo, "show", f"HEAD:{p}")
    pub = _git(public_repo, "show", f"HEAD:{p}")
    res = {"path": p, "reviewed_public_commit": component.get("reviewed_public_commit"),
           "tests_required": component.get("tests_required", [])}
    if priv is None or pub is None:
        res.update(state="MISSING", detail="private" if priv is None else "public")
        return res
    ref, h_priv, h_pub = component["reviewed_fingerprint"], fingerprint(priv), fingerprint(pub)
    if h_priv == h_pub:
        state = "IDENTICAL" if h_pub == ref else "MANIFEST_STALE"   # equal now, but the recorded fingerprint is old
    elif h_pub == ref:
        state = "PRIVATE_AHEAD"
    elif h_priv == ref:
        state = "PUBLIC_AHEAD"
    else:
        state = "DIVERGED"
    differing = 0
    if h_priv != h_pub:   # count only; the text itself is never emitted
        differing = sum(1 for line in difflib.unified_diff(priv.decode("utf-8", "replace").splitlines(),
                                                           pub.decode("utf-8", "replace").splitlines(), n=0)
                        if line[:1] in "+-" and line[:3] not in ("+++", "---"))
    res.update(state=state, private=h_priv[7:19], public=h_pub[7:19], reviewed=ref[7:19], differing_lines=differing,
               worktree_dirty=_git(private_repo, "status", "--porcelain", "--", p) not in (b"", None))
    return res


def run(manifest: Path, private_repo: Path, public_repo: Path = REPO) -> list[dict]:
    results = []
    for c in load_manifest(manifest, public_repo):
        if c["classification"] == "SHARED":
            results.append(compare(c, private_repo, public_repo))
        else:   # SANITIZED / PUBLIC_ONLY differ on purpose: listed, never compared
            results.append({"path": c["path"], "state": "SKIPPED_EXPECTED_DIFFERENCE", "tests_required": []})
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--private", required=True, type=Path, help="path to the private WDBS checkout (read-only use)")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", type=Path, help="write the summary to this file (must be outside both repositories)")
    a = ap.parse_args(argv)
    try:
        if a.record and any(a.record.resolve().is_relative_to(r.resolve()) for r in (REPO, a.private)):
            raise SyncError("--record file must live outside both repositories")
        results = run(a.manifest, a.private, REPO)
    except SyncError as e:
        print(f"sync_check: error: {e}", file=sys.stderr)
        return 2
    if a.record:
        priv_head = (_git(a.private, "rev-parse", "HEAD") or b"").decode().strip()
        a.record.write_text(json.dumps({"private_head": priv_head, "results": results}, indent=1))
    if a.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            extra = f"  private={r['private']} public={r['public']} reviewed={r['reviewed']} differing_lines={r['differing_lines']}" \
                if "private" in r else ""
            dirty = "  (private worktree has uncommitted changes)" if r.get("worktree_dirty") else ""
            print(f"{r['state']:<28} {r['path']}{extra}{dirty}")
        todo = [r for r in results if r["state"] in NEEDS_REVIEW]
        print(f"\n{len(results)} component(s); {len(todo)} need review.")
        for r in todo:
            print(f"  review {r['path']}: run {', '.join(r['tests_required']) or 'the listed tests'} before syncing")
    return 1 if any(r["state"] in NEEDS_REVIEW for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
