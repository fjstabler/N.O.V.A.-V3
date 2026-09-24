"""Shared fixtures.

Every test runs against an isolated ``NOVA_HOME`` so nothing ever touches the
developer's real config, memory database or credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nova.config import SettingsStore, reset_paths_cache
from nova.config.paths import NovaPaths
from nova.context import NovaContext


@pytest.fixture
def nova_home(tmp_path: Path) -> Path:
    previous = os.environ.get("NOVA_HOME")
    os.environ["NOVA_HOME"] = str(tmp_path)
    reset_paths_cache()
    yield tmp_path
    if previous is None:
        os.environ.pop("NOVA_HOME", None)
    else:
        os.environ["NOVA_HOME"] = previous
    reset_paths_cache()


@pytest.fixture
def paths(nova_home: Path) -> NovaPaths:
    return NovaPaths(nova_home, nova_home, nova_home / "cache").ensure()


@pytest.fixture
def store(paths: NovaPaths) -> SettingsStore:
    return SettingsStore(paths)


@pytest.fixture
def ctx(store: SettingsStore) -> NovaContext:
    return NovaContext(store)
