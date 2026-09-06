"""Where N.O.V.A. keeps its files.

Honours XDG on Linux, ``%APPDATA%`` on Windows and Application Support on
macOS. Setting ``NOVA_HOME`` overrides everything, which is what the dev
scripts and the test suite use to stay out of the real user profile.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_DIR_NAME = "nova"


def _base_dirs() -> tuple[Path, Path, Path]:
    """Return ``(config, data, cache)`` roots for the current platform."""
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return appdata / "NOVA", local / "NOVA", local / "NOVA" / "Cache"
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support" / "NOVA"
        return support, support, home / "Library" / "Caches" / "NOVA"
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / APP_DIR_NAME
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / APP_DIR_NAME
    cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / APP_DIR_NAME
    return config, data, cache


@dataclass(frozen=True, slots=True)
class NovaPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def calendar_db(self) -> Path:
        return self.data_dir / "calendar.db"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "nova.log"

    @property
    def plugins_dir(self) -> Path:
        return self.data_dir / "plugins"

    def ensure(self) -> NovaPaths:
        for directory in (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.models_dir,
            self.plugins_dir,
            self.log_file.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        # config_dir holds API keys and integration tokens; data_dir holds
        # conversation memory, calendar contents, enrolled face embeddings and
        # logs — locking the directory itself is enough to keep both out of
        # reach of other local accounts, regardless of individual file modes,
        # since they'd need traverse permission on the directory to get in at all.
        if sys.platform != "win32":
            for directory in (self.config_dir, self.data_dir):
                with contextlib.suppress(OSError):
                    directory.chmod(0o700)
        return self


@lru_cache(maxsize=1)
def get_paths() -> NovaPaths:
    override = os.environ.get("NOVA_HOME")
    if override:
        root = Path(override).expanduser().resolve()
        return NovaPaths(root, root, root / "cache")
    config, data, cache = _base_dirs()
    return NovaPaths(config, data, cache)


def reset_paths_cache() -> None:
    """Test hook — re-reads ``NOVA_HOME``."""
    get_paths.cache_clear()
