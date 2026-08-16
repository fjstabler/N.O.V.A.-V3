"""Desktop session control: open pages and files, launch apps, use the clipboard.

Where :mod:`nova.system.files` and :mod:`nova.system.shell` govern what N.O.V.A.
may touch on the *server*, this governs what it may do on the *desktop it is
sitting on* — the machine with the screen, the browser and the clipboard. It is
what turns "open the invoice", "pull up the weather" and "launch the terminal"
into things that actually happen in front of the user.

Everything here is deliberately reversible. Opening a page, launching an app or
replacing the clipboard can each be undone in a second, so — matching the
"open things freely, confirm the risky stuff" policy — none of these go through
the confirmation gate. Nothing here deletes, overwrites or elevates; a URL is
scheme-checked, a file is opened only after the caller has resolved it inside the
sandbox, and an application name is validated against a strict pattern before it
is ever handed to the OS. Commands run without a shell, so a name can never turn
into two.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

from ..runtime.errors import PermissionDenied, SkillError
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: The only URL schemes we will hand to the browser. `file:` is excluded on
#: purpose — file opening goes through the sandbox — and `javascript:`/`data:`
#: because they execute rather than navigate.
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

#: A launchable application name: a letter or digit, then a bounded run of the
#: characters real app ids and binaries use. No slashes, no whitespace-quoting
#: tricks, no shell metacharacters — those simply do not match.
_SAFE_APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")

#: Standard locations for freedesktop `.desktop` entries, used to confirm an app
#: exists before we optimistically report that it is launching.
_XDG_APP_SUBDIR = "applications"


class DesktopController:
    """Opens URLs, files and applications on the local desktop session.

    ``platform`` and ``wayland`` are injectable so the whole surface is testable
    without a real display server: the tests drive each OS branch by constructing
    the controller with the platform they want to exercise.
    """

    #: Clipboard commands must finish quickly; a hung helper should not wedge a turn.
    CLIPBOARD_TIMEOUT = 5.0
    #: How long to wait for an *opener* (xdg-open/open) to exit before assuming it
    #: handed off successfully and left a long-lived child running.
    OPEN_TIMEOUT = 4.0

    def __init__(self, *, platform: str | None = None, wayland: bool | None = None) -> None:
        self.platform = platform or sys.platform
        self.wayland = wayland if wayland is not None else bool(os.environ.get("WAYLAND_DISPLAY"))

    # ------------------------------------------------------------------- opening

    async def open_url(self, url: str) -> str:
        """Open a web (or mailto) link in the user's default browser."""
        url = url.strip()
        if not url:
            raise SkillError("no URL given")
        # A bare "example.com" or "localhost:3000" is what people actually say, so
        # wrap it in https — but never wrap a scheme-like prefix such as
        # "javascript:" or "data:", which would smuggle an executable URL past the
        # allowlist. host:port (a colon followed by a digit) is not a scheme.
        if url.lower().startswith("mailto:"):
            scheme = "mailto"
        elif "://" in url:
            scheme = url.split("://", 1)[0].lower()
        elif re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url) and not re.match(r"^[^\s:]+:\d", url):
            scheme = url.split(":", 1)[0].lower()
        else:
            url = "https://" + url
            scheme = "https"
        if scheme not in ALLOWED_URL_SCHEMES:
            raise PermissionDenied(f"I can only open http, https or mailto links — not '{scheme}:'")
        await self._open_target(url, fall_back_to_browser=True)
        return f"Opened {url}."

    async def open_path(self, path: Path) -> str:
        """Open an already-resolved file or folder in its default application.

        The caller is responsible for resolving ``path`` inside the file sandbox;
        this method does not itself do containment checks.
        """
        is_dir = await asyncio.to_thread(path.is_dir)
        await self._open_target(str(path), fall_back_to_browser=False)
        kind = "folder" if is_dir else "file"
        return f"Opened the {kind} {path.name}."

    async def launch_app(self, name: str) -> str:
        """Launch a desktop application by name."""
        name = name.strip()
        if not _SAFE_APP_NAME.match(name):
            raise PermissionDenied(f"'{name}' is not a valid application name")
        argv = self._launch_argv(name)
        if argv is None:
            raise SkillError(f"I can't find an application called '{name}' on this machine")
        await self._spawn_detached(argv, wait=0.0)
        return f"Launching {name}."

    # ----------------------------------------------------------------- clipboard

    async def get_clipboard(self) -> str:
        argv = self._clipboard_read_argv()
        if argv is None:
            raise SkillError(self._clipboard_missing_message())
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.CLIPBOARD_TIMEOUT)
        except FileNotFoundError:
            raise SkillError(self._clipboard_missing_message()) from None
        except TimeoutError as exc:
            raise SkillError("the clipboard helper did not respond") from exc
        return stdout.decode("utf-8", "replace")

    async def set_clipboard(self, text: str) -> str:
        argv = self._clipboard_write_argv()
        if argv is None:
            raise SkillError(self._clipboard_missing_message())
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(text.encode("utf-8")), timeout=self.CLIPBOARD_TIMEOUT
            )
        except FileNotFoundError:
            raise SkillError(self._clipboard_missing_message()) from None
        except TimeoutError as exc:
            raise SkillError("the clipboard helper did not respond") from exc
        if proc.returncode:
            detail = stderr.decode("utf-8", "replace").strip()[:200]
            raise SkillError(detail or "could not write to the clipboard")
        return f"Copied {len(text)} characters to the clipboard."

    # -------------------------------------------------------------- OS specifics

    async def _open_target(self, target: str, *, fall_back_to_browser: bool) -> None:
        if self.platform == "win32":
            await asyncio.to_thread(os.startfile, target)  # type: ignore[attr-defined]
            return
        opener = "open" if self.platform == "darwin" else "xdg-open"
        if shutil.which(opener) is None:
            if fall_back_to_browser:
                import webbrowser

                if await asyncio.to_thread(webbrowser.open, target):
                    return
            raise SkillError(f"no desktop opener is available ('{opener}' is not installed)")
        await self._spawn_detached([opener, target], wait=self.OPEN_TIMEOUT)

    def _launch_argv(self, name: str) -> list[str] | None:
        if self.platform == "darwin":
            return ["open", "-a", name]
        if self.platform == "win32":
            return ["cmd", "/c", "start", "", name]
        # Linux/BSD: prefer a real binary on PATH, then a matching .desktop entry
        # launched through gtk-launch. Either way we never touch a shell.
        binary = shutil.which(name)
        if binary is not None:
            return [binary]
        entry = self._find_desktop_entry(name)
        if entry is not None and shutil.which("gtk-launch") is not None:
            return ["gtk-launch", entry]
        return None

    def _find_desktop_entry(self, name: str) -> str | None:
        """Return the id (stem) of a matching ``.desktop`` file, if one exists."""
        wanted = f"{name.lower()}.desktop"
        for base in self._xdg_data_dirs():
            directory = base / _XDG_APP_SUBDIR
            if not directory.is_dir():
                continue
            try:
                for entry in directory.iterdir():
                    if entry.name.lower() == wanted:
                        return entry.stem
            except OSError:
                continue
        return None

    def _xdg_data_dirs(self) -> list[Path]:
        home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        system = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
        dirs = [home, *system.split(":")]
        return [Path(d) for d in dirs if d]

    def _clipboard_read_argv(self) -> list[str] | None:
        if self.platform == "darwin":
            return ["pbpaste"]
        if self.platform == "win32":
            return ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
        if self.wayland and shutil.which("wl-paste") is not None:
            return ["wl-paste", "--no-newline"]
        if shutil.which("xclip") is not None:
            return ["xclip", "-selection", "clipboard", "-o"]
        if shutil.which("xsel") is not None:
            return ["xsel", "--clipboard", "--output"]
        return None

    def _clipboard_write_argv(self) -> list[str] | None:
        if self.platform == "darwin":
            return ["pbcopy"]
        if self.platform == "win32":
            return ["clip"]
        if self.wayland and shutil.which("wl-copy") is not None:
            return ["wl-copy"]
        if shutil.which("xclip") is not None:
            return ["xclip", "-selection", "clipboard"]
        if shutil.which("xsel") is not None:
            return ["xsel", "--clipboard", "--input"]
        return None

    def _clipboard_missing_message(self) -> str:
        if self.platform not in ("darwin", "win32"):
            return "no clipboard tool is installed — install wl-clipboard (Wayland) or xclip (X11)"
        return "the clipboard is not available on this machine"

    # -------------------------------------------------------------------- spawn

    async def _spawn_detached(self, argv: list[str], *, wait: float) -> None:
        """Start a process without a shell and detach from it.

        ``wait`` seconds are given to short-lived launchers (xdg-open, open) so a
        bad target surfaces as an error; long-lived launches pass ``wait=0`` and
        are simply left running in their own session.
        """
        extra = {"start_new_session": True} if self.platform != "win32" else {}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **extra,
            )
        except FileNotFoundError as exc:
            raise SkillError(f"command not found: {exc.filename}") from exc
        except OSError as exc:
            raise SkillError(f"could not start '{argv[0]}': {exc}") from exc

        if wait <= 0:
            return
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=wait)
        except TimeoutError:
            # Still running after the grace period: it launched and took over.
            return
        if proc.returncode:
            detail = stderr.decode("utf-8", "replace").strip()[:200] if stderr else ""
            raise SkillError(detail or f"'{argv[0]}' exited with code {proc.returncode}")
