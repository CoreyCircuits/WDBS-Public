"""Code-review fixes F1 (shared write lock, fail closed, restore only after a write),
F2 (strict replan digest) and F5 (patcher compiles before writing)."""

import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wdbs import fs42_adapter as fs
try:
    from wdbs import fs42_patch
except ImportError:     # withheld from the public export (license review pending); the patcher tests below skip explicitly
    fs42_patch = None
from wdbs import replan as rp

from tests.test_core import DAY0, FixtureCase


class WriteLockTests(FixtureCase):
    def test_second_writer_is_refused(self):
        with rp.write_lock(self.settings):
            env = {k: v for k, v in os.environ.items() if k != rp.LOCK_HELD_ENV}
            code = ("import sys; from wdbs import replan as rp; from wdbs.config import Settings; "
                    "from pathlib import Path\n"
                    "try:\n"
                    f"    with rp.write_lock(Settings(state_db=Path({str(self.settings.state_db)!r}))): print('got')\n"
                    "except rp.ReplanRefused: print('refused')")
            out = subprocess.run(["python3", "-c", code], capture_output=True, text=True, env=env,
                                 cwd=Path(__file__).resolve().parents[1]).stdout.strip()
        self.assertEqual(out, "refused")

    def test_reentrant_for_holder_and_released_after(self):
        with rp.write_lock(self.settings):
            with rp.write_lock(self.settings):  # e.g. overnight -> nested call; must not deadlock
                pass
        self.assertNotIn(rp.LOCK_HELD_ENV, os.environ)

    def test_overnight_running_fails_closed(self):
        with mock.patch.object(rp.subprocess, "run", side_effect=OSError("no bus")):
            self.assertTrue(rp.overnight_running())
        answer = mock.Mock(stdout="")  # e.g. "Failed to connect to bus" on stderr only
        with mock.patch.object(rp.subprocess, "run", return_value=answer):
            self.assertTrue(rp.overnight_running())
        answer = mock.Mock(stdout="inactive\n")
        with mock.patch.object(rp.subprocess, "run", return_value=answer):
            self.assertFalse(rp.overnight_running())


class ReplanSafetyTests(FixtureCase):
    def test_apply_without_digest_is_refused(self):
        now = DAY0 + dt.timedelta(hours=10)
        with mock.patch.object(rp, "overnight_running", return_value=False):
            with self.assertRaises(rp.ReplanRefused):
                self.cp(now).replan(10, DAY0 + dt.timedelta(days=1), apply=True)

    def test_refusal_before_any_write_does_not_restore(self):
        """A restore would erase writes FS42 or another writer made since the backup."""
        now = DAY0 + dt.timedelta(hours=10)
        with fs.connect_ro(self.env.db) as c:
            plan = rp.compute_plan(c, self.station(), now=now, cutoff=DAY0 + dt.timedelta(days=1))
        plan.delete_block_ids = plan.delete_block_ids[:-1]  # stale plan -> refused before the rollback txn
        with mock.patch.object(rp, "overnight_running", return_value=False), \
                mock.patch.object(rp, "sqlite_restore") as restore:
            r = rp.apply(self.settings, plan, now)
        self.assertFalse(r["ok"])
        self.assertFalse(r["restored"])
        restore.assert_not_called()


@unittest.skipIf(fs42_patch is None, "wdbs.fs42_patch is withheld from the public export (license review pending)")
class PatcherCompileFirstTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wdbs-patchtest-"))
        (self.root / "fs42").mkdir()
        self.target = self.root / "fs42" / "mod.py"
        self.target.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_bad_patch_touches_nothing(self):
        good = fs42_patch.Edit("fs42/mod.py", "def a():\n    return 1\n", "def a():\n    return 11  # WDBS\n")
        bad = fs42_patch.Edit("fs42/mod.py", "def b():\n    return 2\n", "def b(:\n    return 2  # WDBS\n")
        before = self.target.read_text()
        with mock.patch.dict(fs42_patch.PATCHES, {"t": [good, bad]}):
            with self.assertRaises(SyntaxError):
                fs42_patch.apply(self.root, "t")
        self.assertEqual(self.target.read_text(), before)
        self.assertFalse(self.target.with_name("mod.py.wdbs-orig").exists())

    def test_good_patch_applies_both_edits_and_keeps_original(self):
        e1 = fs42_patch.Edit("fs42/mod.py", "def a():\n    return 1\n", "def a():\n    return 11  # WDBS\n")
        e2 = fs42_patch.Edit("fs42/mod.py", "def b():\n    return 2\n", "def b():\n    return 22  # WDBS\n")
        before = self.target.read_text()
        with mock.patch.dict(fs42_patch.PATCHES, {"t": [e1, e2]}):
            st = fs42_patch.apply(self.root, "t")
        self.assertTrue(all(v == "applied" for v in st.values()), st)
        self.assertEqual(self.target.with_name("mod.py.wdbs-orig").read_text(), before)


    def test_revert_keeps_other_patches_on_the_same_file(self):
        e1 = fs42_patch.Edit("fs42/mod.py", "def a():\n    return 1\n", "def a():\n    return 11  # WDBS\n")
        e2 = fs42_patch.Edit("fs42/mod.py", "def b():\n    return 2\n", "def b():\n    return 22  # WDBS\n")
        pristine = self.target.read_text()
        with mock.patch.dict(fs42_patch.PATCHES, {"one": [e1], "two": [e2]}):
            fs42_patch.apply(self.root, "one")
            fs42_patch.apply(self.root, "two")
            fs42_patch.revert(self.root, "one")
            text = self.target.read_text()
            self.assertIn("return 22", text)          # patch "two" survives
            self.assertIn("return 1\n", text)
            self.assertTrue(self.target.with_name("mod.py.wdbs-orig").exists())
            fs42_patch.revert(self.root, "two")
        self.assertEqual(self.target.read_text(), pristine)
        self.assertFalse(self.target.with_name("mod.py.wdbs-orig").exists())

    def test_new_binary_files_copy_exactly_and_revert_removes_their_dirs(self):
        overlay = self.root / "overlay"
        (overlay / "static" / "g" / "img").mkdir(parents=True)
        blob = bytes(range(256))
        (overlay / "static/g/img/x.png").write_bytes(blob)
        (overlay / "static/g/index.html").write_text("<p>not python</p>")
        files = [fs42_patch.NewFile("fs42/static/g/img/x.png", "static/g/img/x.png"),
                 fs42_patch.NewFile("fs42/static/g/index.html", "static/g/index.html")]
        with mock.patch.object(fs42_patch, "OVERLAY", overlay), mock.patch.dict(fs42_patch.PATCHES, {"g": files}):
            st = fs42_patch.apply(self.root, "g")
            self.assertTrue(all(v == "applied" for v in st.values()), st)
            self.assertEqual((self.root / "fs42/static/g/img/x.png").read_bytes(), blob)
            fs42_patch.revert(self.root, "g")
        self.assertFalse((self.root / "fs42" / "static").exists())
        self.assertTrue(self.target.exists())


class DateOverrideMergeTests(unittest.TestCase):
    """F4: an engine day must not hide the config's date override for hours it doesn't set."""

    def test_per_hour_merge_and_warning(self):
        from wdbs.fs42_build import merge_date_overrides
        ours = {"October 31": {str(h): {"tags": "alpha"} for h in range(6, 24)}}
        config = {"October 25 - November 2": {"3": {"tags": "horror"}, "20": {"tags": "horror"}},
                  "December 25": {"8": {"tags": "xmas"}}}
        merged, warnings = merge_date_overrides(ours, config, dt.datetime(2026, 10, 31, 6),
                                                dt.datetime(2026, 11, 1, 6))
        day = merged["October 31"]
        self.assertEqual(day["3"], {"tags": "horror"})    # config hour the lineup doesn't set: kept
        self.assertEqual(day["20"], {"tags": "alpha"})    # both set: lineup wins ...
        self.assertEqual(len(warnings), 1)                # ... with a warning
        self.assertIn("20:00", warnings[0])
        self.assertEqual(list(merged)[0], "October 31")   # FS42 takes the first matching key
        self.assertIn("December 25", merged)              # unrelated config overrides untouched
        self.assertIn("October 25 - November 2", merged)  # still applies to the other days


if __name__ == "__main__":
    unittest.main()
