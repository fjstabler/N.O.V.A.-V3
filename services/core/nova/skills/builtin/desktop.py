"""Desktop control: open web pages, files and applications, use the clipboard.

This is what lets N.O.V.A. act on the machine in front of the user rather than
only describe things. "Open YouTube", "pull up that PDF", "launch the terminal",
"copy this to the clipboard" — the everyday errands that make an assistant feel
like it is actually using the computer with you.

Everything here is reversible, so — per the configured policy — it runs without
the confirmation gate. The gate still guards the genuinely risky actions
elsewhere: deleting or overwriting files, shell commands and system-setting
changes all live behind it in the ``server`` skill. Opening a file goes through
the same sandbox those tools use, so N.O.V.A. can only reach paths the operator
has allowed.
"""

from __future__ import annotations

from typing import Annotated

from ...system.desktop import DesktopController
from ...system.files import FileSandbox
from ..base import Param, Skill, tool


class DesktopSkill(Skill):
    name = "desktop"
    description = "Act on this desktop: open web pages, files and apps, and use the clipboard."
    category = "Desktop"

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.desktop.enabled:
            return False, "desktop control disabled in settings"
        return True, ""

    async def setup(self) -> None:
        self.controller = DesktopController()

    @property
    def sandbox(self) -> FileSandbox:
        # Rebuilt from current settings each time so a change to the file roots
        # takes effect without restarting — the same roots the server skill uses.
        return FileSandbox(tuple(self.ctx.settings.server.file_roots))

    # --------------------------------------------------------------------- web

    @tool("Open a web page in the default browser.", mutating=True)
    async def open_web_page(
        self,
        url: Annotated[
            str,
            Param("The address to open", examples=("youtube.com", "https://news.ycombinator.com")),
        ],
    ) -> str:
        return await self.controller.open_url(url)

    @tool("Search the web and open the results in the browser.", mutating=True)
    async def search_the_web(
        self,
        query: Annotated[str, Param("What to search for")],
    ) -> str:
        from urllib.parse import quote_plus

        template = self.ctx.settings.desktop.search_url
        url = template.replace("{query}", quote_plus(query.strip()))
        await self.controller.open_url(url)
        return f"Searching the web for '{query.strip()}'."

    # -------------------------------------------------------------------- files

    @tool("Open a file or folder in its default application.", mutating=True)
    async def open_file(
        self,
        path: Annotated[str, Param("Path to the file or folder")],
    ) -> str:
        resolved = self.sandbox.resolve(path)
        return await self.controller.open_path(resolved)

    # --------------------------------------------------------------------- apps

    @tool("Launch a desktop application by name.", mutating=True)
    async def open_app(
        self,
        name: Annotated[
            str, Param("Application name", examples=("firefox", "gnome-terminal", "code"))
        ],
    ) -> str:
        settings = self.ctx.settings.desktop
        if not settings.allow_launch:
            return "Launching applications is turned off in settings."
        allowlist = [a.strip().lower() for a in settings.app_allowlist if a.strip()]
        if allowlist and name.strip().lower() not in allowlist:
            return (
                f"'{name}' is not in the allowed applications list. "
                "Add it under Desktop settings to launch it."
            )
        return await self.controller.launch_app(name)

    # ---------------------------------------------------------------- clipboard

    @tool("Read the current contents of the clipboard.")
    async def read_clipboard(self) -> str:
        text = await self.controller.get_clipboard()
        if not text.strip():
            return "The clipboard is empty."
        preview = text if len(text) <= 2000 else text[:2000] + "\n… (truncated)"
        return preview

    @tool("Put text on the clipboard, ready to paste.", mutating=True)
    async def copy_to_clipboard(
        self,
        text: Annotated[str, Param("The text to copy")],
    ) -> str:
        return await self.controller.set_clipboard(text)
