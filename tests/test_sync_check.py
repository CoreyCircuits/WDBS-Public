"""sync/sync_check.py: drift states, no source text in output, and private-only files cannot enter the comparison.

Uses throw-away git repositories built in temp dirs; no real private checkout is read.
"""

import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("sync_check", Path(__file__).resolve().parents[1] / "sync" / "sync_check.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

SECRET = "PRIVATE_MARKER_do_not_print"


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo), *args],
                   check=True, capture_output=True)


def _repo(root: Path, files: dict) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    return root


class SyncCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        self.pub = _repo(self.t / "pub", {"a/shared.py": "x = 1\n", "a/other.py": "y = 1\n"})
        self.priv = _repo(self.t / "priv", {"a/shared.py": "x = 1\n", "a/other.py": "y = 1\n",
                                            "secret/only_private.py": f"# {SECRET}\n"})

    def manifest(self, comps):
        for c in comps:
            c.setdefault("tests_required", ["tests.x"])
            if c.get("classification") == "SHARED":
                c.setdefault("reviewed_fingerprint", sc.fingerprint(b"x = 1\n"))
        p = self.t / "manifest.json"
        p.write_text(json.dumps({"components": comps}))
        return p

    def run_cli(self, manifest, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--private", str(self.priv), "--manifest", str(manifest), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self._main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _main(self, argv):
        # main() resolves the public repo from its own location; point it at the temp public repo for the test
        old = sc.REPO
        sc.REPO = self.pub
        try:
            return sc.main(argv)
        finally:
            sc.REPO = old

    def test_identical_exits_zero(self):
        rc, out, _ = self.run_cli(self.manifest([{"path": "a/shared.py", "classification": "SHARED"}]))
        self.assertEqual(rc, 0)
        self.assertIn("IDENTICAL", out)

    def test_private_ahead_needs_review_and_prints_no_source(self):
        (self.priv / "a/shared.py").write_text(f"x = 2  # {SECRET}\n")
        _git(self.priv, "commit", "-qam", "change")
        rc, out, err = self.run_cli(self.manifest([{"path": "a/shared.py", "classification": "SHARED"}]))
        self.assertEqual(rc, 1)
        self.assertIn("PRIVATE_AHEAD", out)
        self.assertIn("differing_lines=2", out)
        self.assertNotIn(SECRET, out + err)
        self.assertIn("tests.x", out)

    def test_public_ahead_diverged_and_stale_states(self):
        m = self.manifest([{"path": "a/shared.py", "classification": "SHARED"}])
        (self.pub / "a/shared.py").write_text("x = 3\n")
        _git(self.pub, "commit", "-qam", "pub change")
        self.assertIn("PUBLIC_AHEAD", self.run_cli(m)[1])
        (self.priv / "a/shared.py").write_text("x = 4\n")
        _git(self.priv, "commit", "-qam", "priv change")
        self.assertIn("DIVERGED", self.run_cli(m)[1])
        (self.priv / "a/shared.py").write_text("x = 3\n")
        _git(self.priv, "commit", "-qam", "priv converge")
        rc, out, _ = self.run_cli(m)
        self.assertIn("MANIFEST_STALE", out)   # equal now, but the reviewed fingerprint is old
        self.assertEqual(rc, 1)

    def test_sanitized_entries_are_listed_not_compared(self):
        (self.priv / "a/other.py").write_text("y = 999\n")
        _git(self.priv, "commit", "-qam", "private-only difference")
        rc, out, _ = self.run_cli(self.manifest([{"path": "a/other.py", "classification": "SANITIZED"}]))
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED_EXPECTED_DIFFERENCE", out)

    def test_unlisted_private_changes_are_ignored(self):
        (self.priv / "a/other.py").write_text("y = 5\n")
        _git(self.priv, "commit", "-qam", "unlisted change")
        rc, out, _ = self.run_cli(self.manifest([{"path": "a/shared.py", "classification": "SHARED"}]))
        self.assertEqual(rc, 0)
        self.assertNotIn("other.py", out)

    def test_private_only_file_cannot_enter_the_flow(self):
        m = self.manifest([{"path": "secret/only_private.py", "classification": "SHARED",
                            "reviewed_fingerprint": sc.fingerprint(f"# {SECRET}\n".encode())}])
        rc, out, err = self.run_cli(m)
        self.assertEqual(rc, 2)
        self.assertIn("not tracked in the public repository", err)
        self.assertNotIn(SECRET, out + err)

    def test_bad_manifest_entries_are_rejected(self):
        for bad in ({"path": "../priv/secret/only_private.py", "classification": "SHARED"},
                    {"path": "/etc/passwd", "classification": "SHARED"},
                    {"path": "a/shared.py", "classification": "PRIVATE_ONLY"},
                    {"path": "a//shared.py", "classification": "SHARED"}):
            rc, _, err = self.run_cli(self.manifest([dict(bad)]))
            self.assertEqual(rc, 2, bad)
            self.assertIn("sync_check: error", err)

    def test_record_file_must_be_outside_both_repos(self):
        m = self.manifest([{"path": "a/shared.py", "classification": "SHARED"}])
        rc, _, err = self.run_cli(m, "--record", str(self.priv / "state.json"))
        self.assertEqual(rc, 2)
        rc, _, _ = self.run_cli(m, "--record", str(self.t / "state.json"))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.t / "state.json").read_text())["results"][0]["state"], "IDENTICAL")

    def test_shipped_manifest_is_valid_and_public_safe(self):
        repo = Path(__file__).resolve().parents[1]
        text = (repo / "sync" / "manifest.json").read_text()
        for c in json.loads(text)["components"]:
            self.assertIn(c["classification"], sc.ALLOWED_CLASSES)
        self.assertNotIn("PRIVATE_ONLY", text)
        self.assertNotIn("/home/", text)


if __name__ == "__main__":
    unittest.main()
