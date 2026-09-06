"""Configuration: typed schema, layered loading, atomic persistence."""

from .paths import NovaPaths, get_paths, reset_paths_cache
from .schema import REDACTED, AnimationQuality, NovaSettings, ThemeName
from .store import SettingsStore
from .ui_schema import describe_settings

__all__ = [
    "REDACTED",
    "AnimationQuality",
    "NovaPaths",
    "NovaSettings",
    "SettingsStore",
    "ThemeName",
    "describe_settings",
    "get_paths",
    "reset_paths_cache",
]
