"""Public-export guards: commands that depend on modules withheld from the export must say so, not crash.

The modules are made to look absent by patching importlib's spec lookup, so this test passes both in the public
tree (really absent) and in a tree where they are present (guard path still exercised, nothing is deleted).
"""

import contextlib
import importlib.util
import io
import unittest
from unittest import mock

from wdbs import cli
from wdbs.api import ControlPlane

_real_find_spec = importlib.util.find_spec
WITHHELD = ("wdbs.fs42_patch", "wdbs.migrate_paths")


def _find_spec(name, *args, **kwargs):
    return None if name in WITHHELD else _real_find_spec(name, *args, **kwargs)


class WithheldModuleGuardTests(unittest.TestCase):
    def test_patch_status_reports_unsupported(self):
        with mock.patch("importlib.util.find_spec", side_effect=_find_spec):
            r = object.__new__(ControlPlane).fs42_patch_status()
        self.assertIs(r["supported"], False)
        self.assertIn("not included", r["error"])

    def test_player_patch_status_command_exits_nonzero_with_message(self):
        cp = mock.Mock()
        cp.fs42_patch_status.return_value = {"supported": False, "error": "wdbs.fs42_patch is not included in this export"}
        args = mock.Mock(action="patch-status", json=False, channel=None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_player(cp, args)
        self.assertEqual(rc, 1)
        self.assertIn("not included", out.getvalue())

    def test_migrate_paths_command_exits_2_when_module_absent(self):
        with mock.patch("importlib.util.find_spec", side_effect=_find_spec), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cli.main(["migrate-paths"])
        self.assertEqual(rc, 2)
        self.assertIn("not included", err.getvalue())


if __name__ == "__main__":
    unittest.main()
