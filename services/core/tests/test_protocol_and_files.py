"""Wire protocol encoding and filesystem sandbox containment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nova.runtime.errors import PermissionDenied
from nova.system.files import FileSandbox
from nova.transport.protocol import PROTOCOL_VERSION, Kind, Message

# ------------------------------------------------------------------- protocol


def test_messages_round_trip() -> None:
    original = Message.event("state.changed", {"state": "idle", "previous": "thinking"})
    decoded = Message.decode(original.encode())
    assert decoded.kind is Kind.EVENT
    assert decoded.topic == "state.changed"
    assert decoded.payload["state"] == "idle"
    assert decoded.id == original.id


def test_responses_carry_the_request_id() -> None:
    response = Message.response("req-123", "settings.get", {"settings": {}})
    assert response.id == "req-123"
    assert response.kind is Kind.RESPONSE


def test_errors_include_a_machine_readable_code() -> None:
    error = Message.error("req-1", "voice.activate", "nova.unavailable", "no microphone")
    payload = json.loads(error.encode())["payload"]
    assert payload["code"] == "nova.unavailable"
    assert payload["message"] == "no microphone"


def test_version_mismatches_are_rejected() -> None:
    raw = json.dumps({"v": 99, "kind": "event", "topic": "x", "payload": {}})
    with pytest.raises(ValueError, match="unsupported protocol version"):
        Message.decode(raw)


def test_malformed_messages_are_rejected() -> None:
    with pytest.raises(ValueError):
        Message.decode(json.dumps({"v": PROTOCOL_VERSION, "kind": "nonsense", "topic": "x"}))
    with pytest.raises(ValueError, match="payload must be an object"):
        Message.decode(
            json.dumps({"v": PROTOCOL_VERSION, "kind": "event", "topic": "x", "payload": [1, 2]})
        )
    with pytest.raises(ValueError):
        Message.decode(json.dumps(["not", "an", "object"]))


def test_unserialisable_values_do_not_break_encoding() -> None:
    """A skill can return anything; the socket must survive it."""

    class Custom:
        def __repr__(self) -> str:
            return "<custom>"

    encoded = Message.event("x", {"value": Custom(), "set": {1, 2}}).encode()
    payload = json.loads(encoded)["payload"]
    assert payload["value"] == "<custom>"
    assert sorted(payload["set"]) == [1, 2]


# -------------------------------------------------------------------- sandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> FileSandbox:
    root = tmp_path / "workspace"
    (root / "nested").mkdir(parents=True)
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    return FileSandbox((str(root),))


def test_paths_inside_the_root_resolve(sandbox: FileSandbox, tmp_path: Path) -> None:
    assert sandbox.resolve(str(tmp_path / "workspace" / "notes.txt")).name == "notes.txt"


def test_relative_paths_resolve_against_the_first_root(sandbox: FileSandbox) -> None:
    assert sandbox.resolve("notes.txt").name == "notes.txt"


def test_traversal_out_of_the_sandbox_is_refused(sandbox: FileSandbox) -> None:
    with pytest.raises(PermissionDenied, match="outside the permitted roots"):
        sandbox.resolve("../../../../etc/passwd")


def test_absolute_paths_outside_the_sandbox_are_refused(sandbox: FileSandbox) -> None:
    with pytest.raises(PermissionDenied, match="outside the permitted roots"):
        sandbox.resolve("/etc/hostname")


def test_symlinks_cannot_escape_the_sandbox(sandbox: FileSandbox, tmp_path: Path) -> None:
    """Resolution follows links before the containment check, so this is caught."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "workspace" / "escape.txt"
    link.symlink_to(outside)

    with pytest.raises(PermissionDenied, match="outside the permitted roots"):
        sandbox.resolve(str(link))


def test_credential_directories_are_always_denied(tmp_path: Path) -> None:
    root = tmp_path / "home"
    (root / ".ssh").mkdir(parents=True)
    (root / ".ssh" / "id_rsa").write_text("KEY", encoding="utf-8")
    sandbox = FileSandbox((str(root),))

    with pytest.raises(PermissionDenied, match="credentials"):
        sandbox.resolve(str(root / ".ssh" / "id_rsa"))


@pytest.mark.parametrize(
    "name",
    [
        ".git-credentials",
        ".npmrc",
        ".pgpass",
        ".my.cnf",
        "id_ecdsa",  # not literally id_rsa/id_ed25519, but still an SSH key
        "deploy_ed25519",
        "server.pem",
        "client.p12",
    ],
)
def test_credential_file_patterns_beyond_the_exact_name_list_are_denied(
    tmp_path: Path, name: str
) -> None:
    """DENIED_NAMES alone only catches a fixed set of exact filenames — a key
    saved under any other name, or a well-known credential file with no single
    canonical name, needs pattern matching to be caught at all."""
    root = tmp_path / "home"
    root.mkdir()
    (root / name).write_text("secret", encoding="utf-8")
    sandbox = FileSandbox((str(root),))

    with pytest.raises(PermissionDenied, match="credentials"):
        sandbox.resolve(str(root / name))


def test_missing_files_are_reported(sandbox: FileSandbox) -> None:
    with pytest.raises(PermissionDenied, match="no such file"):
        sandbox.resolve("does-not-exist.txt")
    # …unless we are about to create it.
    assert sandbox.resolve("new-file.txt", must_exist=False).name == "new-file.txt"


async def test_reading_and_writing(sandbox: FileSandbox) -> None:
    await sandbox.write("output.txt", "written by nova")
    assert await sandbox.read("output.txt") == "written by nova"


async def test_appending_preserves_existing_content(sandbox: FileSandbox) -> None:
    await sandbox.write("log.txt", "first\n")
    await sandbox.write("log.txt", "second\n", append=True)
    assert await sandbox.read("log.txt") == "first\nsecond\n"


async def test_large_files_are_truncated_not_loaded_whole(sandbox: FileSandbox) -> None:
    await sandbox.write("big.txt", "x" * 5000)
    content = await sandbox.read("big.txt", max_bytes=1000)
    assert len(content) < 1200
    assert "truncated" in content


async def test_listing_hides_credential_entries(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    (root / "readme.md").write_text("hi", encoding="utf-8")
    (root / ".ssh").mkdir()
    sandbox = FileSandbox((str(root),))

    names = {entry.name for entry in await sandbox.list_dir(str(root))}
    assert names == {"readme.md"}


async def test_grep_finds_matching_lines(sandbox: FileSandbox) -> None:
    await sandbox.write("nested/app.log", "INFO start\nERROR disk full\nINFO done\n")
    matches = await sandbox.grep(".", "error")
    assert len(matches) == 1
    assert "disk full" in matches[0]


async def test_deleting_a_sandbox_root_is_refused(sandbox: FileSandbox, tmp_path: Path) -> None:
    with pytest.raises(PermissionDenied, match="sandbox root"):
        await sandbox.delete(str(tmp_path / "workspace"), recursive=True)


async def test_deleting_a_directory_requires_the_recursive_flag(sandbox: FileSandbox) -> None:
    with pytest.raises(PermissionDenied, match="recursive"):
        await sandbox.delete("nested")
