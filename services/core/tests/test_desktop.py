"""Desktop control: opening pages/files/apps and the clipboard, per platform.

The controller shells out to the OS, so the tests fake ``create_subprocess_exec``
and ``shutil.which`` and drive each platform branch by constructing the
controller with the platform it should target — no real display server, browser
or clipboard tool required. Two things are pinned down hardest: that a URL scheme
is validated before anything is launched, and that an application name is
strictly patterned so it can never smuggle a second command past ``exec``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nova.context import NovaContext
from nova.runtime.errors import PermissionDenied, SkillError
from nova.skills.builtin.desktop import DesktopSkill
from nova.system import desktop as desktop_module
from nova.system.desktop import DesktopController

# ------------------------------------------------------------------ fake OS


class FakeProc:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdin_input: bytes | None = None

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_input = stdin
        return self._stdout, self._stderr


class SpawnRecorder:
    """Stands in for ``asyncio.create_subprocess_exec`` and records every call."""

    def __init__(self, **proc_kwargs: Any) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self._proc_kwargs = proc_kwargs

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProc:
        self.calls.append((argv, kwargs))
        return FakeProc(**self._proc_kwargs)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.calls[-1][0]


def which_map(mapping: dict[str, str | None]):
    """A fake ``shutil.which`` backed by a dict; anything unlisted is missing."""

    def which(name: str) -> str | None:
        return mapping.get(name)

    return which


def set_which(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str | None]) -> None:
    monkeypatch.setattr(desktop_module.shutil, "which", which_map(mapping))


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> SpawnRecorder:
    recorder = SpawnRecorder()
    monkeypatch.setattr(desktop_module.asyncio, "create_subprocess_exec", recorder)
    return recorder


# --------------------------------------------------------------------- URLs


async def test_open_url_rejects_non_web_schemes(spawn: SpawnRecorder) -> None:
    controller = DesktopController(platform="linux")
    for bad in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi"]:
        with pytest.raises(PermissionDenied):
            await controller.open_url(bad)
    assert spawn.calls == []  # nothing was ever launched


async def test_open_url_assumes_https_for_a_bare_domain(
    spawn: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_which(monkeypatch, {"xdg-open": "/usr/bin/xdg-open"})
    controller = DesktopController(platform="linux")

    result = await controller.open_url("youtube.com")

    assert spawn.argv == ("xdg-open", "https://youtube.com")
    assert "https://youtube.com" in result


async def test_open_url_uses_the_macos_opener(
    spawn: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_which(monkeypatch, {"open": "/usr/bin/open"})
    controller = DesktopController(platform="darwin")

    await controller.open_url("https://example.com")

    assert spawn.argv == ("open", "https://example.com")


async def test_open_url_falls_back_to_webbrowser_when_no_opener(
    spawn: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webbrowser

    set_which(monkeypatch, {})  # no xdg-open
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    controller = DesktopController(platform="linux")

    await controller.open_url("https://example.com")

    assert opened == ["https://example.com"]
    assert spawn.calls == []  # never tried to spawn a missing opener


# -------------------------------------------------------------------- apps


async def test_launch_app_rejects_unsafe_names(spawn: SpawnRecorder) -> None:
    controller = DesktopController(platform="linux")
    for bad in ["../evil", "rm -rf; firefox", "foo/bar", "$(reboot)"]:
        with pytest.raises(PermissionDenied):
            await controller.launch_app(bad)
    assert spawn.calls == []


async def test_launch_app_runs_a_binary_on_path(
    spawn: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_which(monkeypatch, {"firefox": "/usr/bin/firefox"})
    controller = DesktopController(platform="linux")

    result = await controller.launch_app("firefox")

    assert spawn.argv == ("/usr/bin/firefox",)
    assert spawn.calls[-1][1].get("start_new_session") is True  # detached
    assert "firefox" in result


async def test_launch_app_falls_back_to_a_desktop_entry(
    spawn: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        desktop_module.shutil, "which", which_map({"gtk-launch": "/usr/bin/gtk-launch"})
    )
    controller = DesktopController(platform="linux")
    monkeypatch.setattr(controller, "_find_desktop_entry", lambda name: "code")

    await controller.launch_app("code")

    assert spawn.argv == ("gtk-launch", "code")


async def test_launch_app_reports_a_missing_application(
    spawn: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_which(monkeypatch, {})
    controller = DesktopController(platform="linux")
    monkeypatch.setattr(controller, "_find_desktop_entry", lambda name: None)

    with pytest.raises(SkillError, match="can't find an application"):
        await controller.launch_app("nonesuch")


async def test_launch_app_uses_open_dash_a_on_macos(
    spawn: SpawnRecorder,
) -> None:
    controller = DesktopController(platform="darwin")
    await controller.launch_app("Visual Studio Code")
    assert spawn.argv == ("open", "-a", "Visual Studio Code")


def test_find_desktop_entry_scans_xdg_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "code.desktop").write_text("[Desktop Entry]\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", "")
    controller = DesktopController(platform="linux")

    assert controller._find_desktop_entry("code") == "code"
    assert controller._find_desktop_entry("CODE") == "code"  # case-insensitive
    assert controller._find_desktop_entry("missing") is None


# --------------------------------------------------------------- clipboard


async def test_get_clipboard_reads_via_xclip(
    spawn: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_which(monkeypatch, {"xclip": "/usr/bin/xclip"})

    async def reader(*argv: str, **kwargs: Any) -> FakeProc:
        spawn.calls.append((argv, kwargs))
        return FakeProc(stdout=b"hello world")

    monkeypatch.setattr(desktop_module.asyncio, "create_subprocess_exec", reader)
    controller = DesktopController(platform="linux", wayland=False)

    text = await controller.get_clipboard()

    assert text == "hello world"
    assert spawn.argv == ("xclip", "-selection", "clipboard", "-o")


async def test_set_clipboard_writes_via_wl_copy_on_wayland(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_which(monkeypatch, {"wl-copy": "/usr/bin/wl-copy"})
    captured: dict[str, Any] = {}

    async def writer(*argv: str, **kwargs: Any) -> FakeProc:
        proc = FakeProc()
        captured["argv"] = argv
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(desktop_module.asyncio, "create_subprocess_exec", writer)
    controller = DesktopController(platform="linux", wayland=True)

    result = await controller.set_clipboard("copy me")

    assert captured["argv"] == ("wl-copy",)
    assert captured["proc"].stdin_input == b"copy me"
    assert "7 characters" in result


async def test_clipboard_degrades_when_no_tool_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_which(monkeypatch, {})
    controller = DesktopController(platform="linux", wayland=False)

    with pytest.raises(SkillError, match="clipboard tool"):
        await controller.get_clipboard()
    with pytest.raises(SkillError, match="clipboard tool"):
        await controller.set_clipboard("x")


async def test_open_reports_a_launcher_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_which(monkeypatch, {"xdg-open": "/usr/bin/xdg-open"})

    async def failing(*argv: str, **kwargs: Any) -> FakeProc:
        return FakeProc(returncode=3, stderr=b"no application found")

    monkeypatch.setattr(desktop_module.asyncio, "create_subprocess_exec", failing)
    controller = DesktopController(platform="linux")

    with pytest.raises(SkillError, match="no application found"):
        await controller.open_url("https://example.com")


# ------------------------------------------------------------------- skill


class FakeController:
    def __init__(self) -> None:
        self.opened_urls: list[str] = []
        self.opened_paths: list[Path] = []
        self.launched: list[str] = []

    async def open_url(self, url: str) -> str:
        self.opened_urls.append(url)
        return f"Opened {url}."

    async def open_path(self, path: Path) -> str:
        self.opened_paths.append(path)
        return f"Opened {path.name}."

    async def launch_app(self, name: str) -> str:
        self.launched.append(name)
        return f"Launching {name}."


def make_skill(ctx: NovaContext) -> tuple[DesktopSkill, FakeController]:
    skill = DesktopSkill(ctx)
    fake = FakeController()
    skill.controller = fake  # type: ignore[assignment]
    return skill, fake


async def test_skill_is_unavailable_when_disabled(ctx: NovaContext) -> None:
    ctx.store.patch({"desktop": {"enabled": False}}, persist=False)
    available, reason = DesktopSkill(ctx).is_available()
    assert available is False
    assert "disabled" in reason


async def test_open_file_resolves_within_the_sandbox(ctx: NovaContext, tmp_path: Path) -> None:
    target = tmp_path / "invoice.pdf"
    target.write_text("x")
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)
    skill, fake = make_skill(ctx)

    await skill.open_file(path="invoice.pdf")

    assert fake.opened_paths == [target]


async def test_open_file_refuses_a_path_outside_the_sandbox(
    ctx: NovaContext, tmp_path: Path
) -> None:
    ctx.store.patch({"server": {"file_roots": [str(tmp_path)]}}, persist=False)
    skill, _ = make_skill(ctx)

    with pytest.raises(PermissionDenied):
        await skill.open_file(path="/etc/hosts")


async def test_search_the_web_builds_the_query_url(ctx: NovaContext) -> None:
    skill, fake = make_skill(ctx)
    await skill.search_the_web(query="best pizza near me")
    assert fake.opened_urls == ["https://duckduckgo.com/?q=best+pizza+near+me"]


async def test_open_app_respects_the_disable_switch(ctx: NovaContext) -> None:
    ctx.store.patch({"desktop": {"allow_launch": False}}, persist=False)
    skill, fake = make_skill(ctx)

    result = await skill.open_app(name="firefox")

    assert "turned off" in result.lower()
    assert fake.launched == []


async def test_open_app_enforces_the_allowlist(ctx: NovaContext) -> None:
    ctx.store.patch({"desktop": {"app_allowlist": ["firefox"]}}, persist=False)
    skill, fake = make_skill(ctx)

    blocked = await skill.open_app(name="gnome-terminal")
    assert "not in the allowed" in blocked
    assert fake.launched == []

    await skill.open_app(name="Firefox")  # case-insensitive match
    assert fake.launched == ["Firefox"]


def test_every_desktop_tool_is_reversible_and_never_gated(ctx: NovaContext) -> None:
    # None of these should be destructive: opening things runs freely, matching
    # the configured "confirm only the risky stuff" policy.
    specs = {s.name: s for s in DesktopSkill(ctx).collect_tools()}
    assert set(specs) == {
        "open_web_page",
        "search_the_web",
        "open_file",
        "open_app",
        "read_clipboard",
        "copy_to_clipboard",
    }
    assert not any(s.destructive for s in specs.values())


def test_reading_the_clipboard_is_not_even_a_mutation(ctx: NovaContext) -> None:
    specs = {s.name: s for s in DesktopSkill(ctx).collect_tools()}
    assert specs["read_clipboard"].mutating is False
    assert specs["copy_to_clipboard"].mutating is True
