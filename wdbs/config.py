"""WDBS control-plane settings.

Everything is resolved from environment variables with defaults that match the
production TV host layout. Nothing here reads secrets.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass(frozen=True)
class Settings:
    fs42_root: Path = field(default_factory=lambda: _env_path("WDBS_FS42_ROOT", "/path/to/FieldStation42"))
    state_db: Path = field(default_factory=lambda: _env_path("WDBS_STATE_DB", "~/.local/state/wdbs/wdbs.db"))
    manifest: Path = field(default_factory=lambda: _env_path(
        "WDBS_MANIFEST", "/srv/example-media/wdbs/control/channel10_rotation.json"))
    # Where FS42 DB backups go. Matches the existing wdbs_* backup convention.
    backup_dir: Path | None = None
    # FS42 DB override, used by tests and rehearsals on a copy.
    fs42_db_override: Path | None = None

    @property
    def fs42_main_config(self) -> dict:
        path = self.fs42_root / "confs" / "main_config.json"
        if path.exists():
            try:
                # FS42 merges top-level keys of main_config.json into server_conf.
                return json.loads(path.read_text()) or {}
            except (OSError, ValueError):
                return {}
        return {}

    def _fs42_path(self, key: str, default: str) -> Path:
        value = self.fs42_main_config.get(key, default)
        p = Path(value)
        return p if p.is_absolute() else self.fs42_root / p

    @property
    def fs42_db(self) -> Path:
        if self.fs42_db_override:
            return self.fs42_db_override
        env = os.environ.get("WDBS_FS42_DB")
        if env:
            return Path(env).expanduser()
        return self._fs42_path("db_path", "runtime/fs42_fluid.db")

    @property
    def status_socket(self) -> Path:
        return self._fs42_path("status_socket", "runtime/play_status.socket")

    @property
    def confs_dir(self) -> Path:
        return self.fs42_root / "confs"

    @property
    def backups(self) -> Path:
        return self.backup_dir or self.fs42_db.parent

    def with_fs42_db(self, path: Path) -> "Settings":
        return Settings(self.fs42_root, self.state_db, self.manifest, self.backup_dir, Path(path))
