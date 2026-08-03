"""Settings layering, secret handling and persistence."""

from __future__ import annotations

import os

import pytest

from nova.config import REDACTED, SettingsStore, describe_settings
from nova.config.paths import NovaPaths
from nova.runtime.errors import ConfigError


def test_defaults_are_usable_without_a_config_file(store: SettingsStore) -> None:
    settings = store.settings
    assert settings.assistant.name == "NOVA"
    assert settings.transport.host == "127.0.0.1"
    assert settings.voice.wake.phrase == "hey nova"


def test_a_transport_token_is_generated_on_first_run(store: SettingsStore) -> None:
    """The UI cannot connect without one, so it must never be empty."""
    assert len(store.settings.transport.token) >= 32


def test_patch_persists_and_survives_a_reload(paths: NovaPaths) -> None:
    store = SettingsStore(paths)
    store.patch({"appearance": {"theme": "ember", "animation_quality": "ultra"}})

    reopened = SettingsStore(paths)
    assert reopened.settings.appearance.theme == "ember"
    assert reopened.settings.appearance.animation_quality == "ultra"


def test_patch_is_a_deep_merge_not_a_replacement(store: SettingsStore) -> None:
    store.patch({"voice": {"tts": {"speed": 1.4}}})
    assert store.settings.voice.tts.speed == 1.4
    # Siblings in the same nested table must be untouched.
    assert store.settings.voice.tts.voice == "af_sarah"
    assert store.settings.voice.wake.phrase == "hey nova"


def test_secrets_are_redacted_before_leaving_the_process(store: SettingsStore) -> None:
    store.patch({"openai": {"api_key": "sk-super-secret"}})
    public = store.public_dict()
    assert public["openai"]["api_key"] == REDACTED
    assert "sk-super-secret" not in str(public)


def test_round_tripping_a_redacted_document_preserves_the_secret(store: SettingsStore) -> None:
    """The panel edits the whole document; it must not wipe what it cannot see."""
    store.patch({"openai": {"api_key": "sk-real-key", "temperature": 0.5}})
    document = store.public_dict()
    document["openai"]["temperature"] = 0.9  # user changes something unrelated

    store.patch(document)

    assert store.settings.openai.api_key == "sk-real-key"
    assert store.settings.openai.temperature == 0.9


def test_secrets_are_found_in_nested_lists(store: SettingsStore) -> None:
    store.patch(
        {
            "homelab": {
                "services": [
                    {"kind": "jellyfin", "name": "Jellyfin", "url": "http://x", "api_key": "abc123"}
                ]
            }
        }
    )
    public = store.public_dict()
    assert public["homelab"]["services"][0]["api_key"] == REDACTED
    assert public["homelab"]["services"][0]["name"] == "Jellyfin"


def test_environment_variables_override_the_file(paths: NovaPaths) -> None:
    os.environ["NOVA_OPENAI__MODEL"] = "gpt-4o-mini"
    os.environ["NOVA_APPEARANCE__GPU_ACCELERATION"] = "false"
    try:
        store = SettingsStore(paths)
        assert store.settings.openai.model == "gpt-4o-mini"
        assert store.settings.appearance.gpu_acceleration is False
    finally:
        del os.environ["NOVA_OPENAI__MODEL"]
        del os.environ["NOVA_APPEARANCE__GPU_ACCELERATION"]


def test_invalid_patches_are_rejected_with_a_readable_error(store: SettingsStore) -> None:
    with pytest.raises(ConfigError):
        store.patch({"voice": {"tts": {"speed": 99.0}}})
    assert store.settings.voice.tts.speed == 1.05  # unchanged


def test_a_corrupt_section_falls_back_instead_of_bricking_startup(paths: NovaPaths) -> None:
    """A bad hand-edit should cost that section, not the whole assistant."""
    paths.config_file.write_text(
        '[appearance]\ntheme = "not-a-real-theme"\n\n[assistant]\nname = "JARVIS"\n',
        encoding="utf-8",
    )
    store = SettingsStore(paths)
    assert store.settings.appearance.theme == "nova-blue"  # defaulted
    assert store.settings.assistant.name == "JARVIS"  # preserved


def test_unparseable_config_raises_clearly(paths: NovaPaths) -> None:
    paths.config_file.write_text("this is not [valid toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        SettingsStore(paths)


def test_change_listeners_receive_the_changed_paths(store: SettingsStore) -> None:
    seen: list[dict] = []
    store.on_change(lambda settings, changed: seen.append(changed))
    store.patch({"appearance": {"theme": "aurora"}})
    assert seen and "appearance.theme" in seen[0]
    assert seen[0]["appearance.theme"] == "aurora"


def test_config_file_is_not_world_readable(store: SettingsStore) -> None:
    """It holds the OpenAI key and every integration token."""
    store.patch({"openai": {"api_key": "sk-x"}})
    mode = store.paths.config_file.stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"config.toml is readable by others: {oct(mode)}"


# ------------------------------------------------------------------ ui schema


def test_settings_schema_covers_every_section() -> None:
    sections = describe_settings()
    keys = {s["key"] for s in sections}
    for expected in ("openai", "voice", "appearance", "home_assistant", "developer"):
        assert expected in keys


def test_schema_marks_secret_fields() -> None:
    sections = {s["key"]: s for s in describe_settings()}
    api_key = next(f for f in sections["openai"]["fields"] if f["key"] == "api_key")
    assert api_key["secret"] is True
    assert api_key["control"] == "password"
    assert "default" not in api_key  # never ship a secret's default to the UI


def test_schema_derives_controls_from_types() -> None:
    sections = {s["key"]: s for s in describe_settings()}
    fields = {f["key"]: f for f in sections["appearance"]["fields"]}

    assert fields["gpu_acceleration"]["control"] == "toggle"
    assert fields["theme"]["control"] == "select"
    assert {o["value"] for o in fields["theme"]["options"]} >= {"nova-blue", "ember"}
    assert fields["core_scale"]["control"] == "slider"
    assert fields["core_scale"]["min"] == pytest.approx(0.5)
    assert fields["core_scale"]["max"] == pytest.approx(1.6)


def test_schema_handles_lists_of_groups() -> None:
    sections = {s["key"]: s for s in describe_settings()}
    services = next(f for f in sections["homelab"]["fields"] if f["key"] == "services")
    assert services["control"] == "list-of-groups"
    assert {f["key"] for f in services["fields"]} >= {"kind", "url", "api_key"}


# ------------------------------------------------- environment precedence


def test_environment_overrides_are_recorded_by_path(paths: NovaPaths) -> None:
    """An env var silently outranking the panel is a trap; it must be visible."""
    os.environ["NOVA_OPENAI__API_KEY"] = "sk-from-environment"
    try:
        store = SettingsStore(paths)
        assert "openai.api_key" in store.env_overrides
        assert store.settings.openai.api_key == "sk-from-environment"
    finally:
        del os.environ["NOVA_OPENAI__API_KEY"]


def test_no_overrides_means_an_empty_set(store: SettingsStore) -> None:
    assert store.env_overrides == frozenset()


def test_the_environment_keeps_precedence_after_a_save(paths: NovaPaths) -> None:
    """Precedence must be the same at runtime as at load, or it is unexplainable."""
    os.environ["NOVA_OPENAI__API_KEY"] = "sk-from-environment"
    try:
        store = SettingsStore(paths)
        store.patch({"openai": {"api_key": "sk-from-panel"}})
        assert store.settings.openai.api_key == "sk-from-environment"
    finally:
        del os.environ["NOVA_OPENAI__API_KEY"]

    # The panel's value was still saved, and applies once the variable is gone.
    assert SettingsStore(paths).settings.openai.api_key == "sk-from-panel"


def test_an_environment_secret_is_never_written_to_disk(paths: NovaPaths) -> None:
    """Writing it would defeat the point of supplying it via the environment."""
    os.environ["NOVA_OPENAI__API_KEY"] = "sk-secret-from-environment"
    try:
        store = SettingsStore(paths)
        # An unrelated edit still triggers a full write of the document.
        store.patch({"appearance": {"theme": "ember"}})
    finally:
        del os.environ["NOVA_OPENAI__API_KEY"]

    on_disk = paths.config_file.read_text(encoding="utf-8")
    assert "sk-secret-from-environment" not in on_disk
    assert "ember" in on_disk
