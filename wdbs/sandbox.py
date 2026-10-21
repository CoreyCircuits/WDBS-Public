"""Throwaway FS42 roots for rehearsing FS42-side changes without touching production.

A sandbox gets its own copy of the FS42 code (fs42/, field_player.py, station_42.py,
tools/), confs/, and runtime/fs42_fluid.db (SQLite backup API). The catalog
directory is a symlink to the real one: media is only ever read. Nothing in a
sandbox writes to the live FS42 root.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .config import Settings
from .replan import sqlite_copy

CODE = ("fs42", "field_player.py", "station_42.py", "tools")


def make_sandbox(settings: Settings, parent: Path | None = None) -> Path:
    live = settings.fs42_root
    base = Path(tempfile.mkdtemp(prefix="wdbs-sandbox-", dir=str(parent) if parent else None))
    root = base / "FieldStation42"
    root.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in CODE:
        src = live / name
        if src.is_dir():
            shutil.copytree(src, root / name, ignore=ignore)
        elif src.exists():
            shutil.copy2(src, root / name)
    shutil.copytree(live / "confs", root / "confs", ignore=shutil.ignore_patterns("*.bak"))
    (root / "runtime").mkdir()
    sqlite_copy(settings.fs42_db, root / "runtime" / "fs42_fluid.db")
    for f in ("channel.socket", "play_status.socket"):
        (root / "runtime" / f).write_text("")
    if (live / "catalog").exists():
        (root / "catalog").symlink_to(live / "catalog")
    return root


def remove_sandbox(root: Path):
    root = Path(root)
    if root.name == "FieldStation42" and root.parent.name.startswith("wdbs-sandbox-"):
        shutil.rmtree(root.parent, ignore_errors=True)
    else:
        raise ValueError(f"refusing to remove non-sandbox path {root}")
