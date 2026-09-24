"""Assistant self-management: time, its own state, notifications and settings.

These are the tools that let N.O.V.A. answer questions about itself and adjust
its own behaviour on request ("speak a bit slower", "turn on do not disturb")
without the user ever opening the settings panel.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...notifications import Level, Notification, NotificationService
from ...runtime import Topics
from ..base import Param, Skill, tool


class AssistantSkill(Skill):
    name = "assistant"
    description = "Time, N.O.V.A.'s own status, notifications and settings."
    category = "Assistant"

    @tool("Get the current time and date.")
    async def current_time(
        self,
        timezone: Annotated[
            str, Param("IANA timezone; empty for local", examples=("Europe/London",))
        ] = "",
    ) -> str:
        now = datetime.now()
        if timezone:
            try:
                now = datetime.now(ZoneInfo(timezone))
            except (ZoneInfoNotFoundError, ValueError):
                return f"I don't recognise the timezone '{timezone}'."
        elif self.ctx.settings.assistant.timezone:
            with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
                now = datetime.now(ZoneInfo(self.ctx.settings.assistant.timezone))
        return now.strftime("%H:%M on %A %d %B %Y")

    @tool("Report which of N.O.V.A.'s own subsystems are running or degraded.")
    async def self_status(self) -> str:
        lines: list[str] = []
        for report in self.ctx.services.health_report():
            state = report["state"]
            detail = f" — {report['detail']}" if report.get("detail") else ""
            lines.append(f"{report['name']}: {state}{detail}")
        return "\n".join(lines)

    @tool("List the capabilities currently available, grouped by skill.")
    async def list_capabilities(self) -> str:
        registry = self.ctx.skills
        if registry is None:
            return "No skills are loaded."
        lines: list[str] = []
        for info in registry.catalogue():
            if info.available:
                lines.append(f"{info.name}: {len(info.tools)} tools — {info.description}")
            else:
                lines.append(f"{info.name}: unavailable ({info.reason})")
        return "\n".join(lines)

    @tool("Show a notification panel on screen.", mutating=True)
    async def notify(
        self,
        title: Annotated[str, Param("Panel heading")],
        body: Annotated[str, Param("Supporting text")] = "",
        level: Annotated[
            Literal["info", "success", "warning", "critical"], Param("Visual weight")
        ] = "info",
    ) -> str:
        service = self.ctx.service("notifications", NotificationService)
        notification = Notification(title=title, body=body, level=Level(level), source="assistant")
        if service is not None:
            await service.raise_notification(notification)
        else:
            self.ctx.bus.publish(Topics.NOTIFICATION, notification.as_payload(), source="assistant")
        return "Shown."

    @tool(
        "Send the user a notification, routed to actually reach them: spoken aloud "
        "if they're in the room, or pushed to their phone if they're away. Use this "
        "for anything you want to tell them proactively — a reminder, a task finishing, "
        "something worth flagging — not for answering the current turn.",
        mutating=True,
    )
    async def reach_me(
        self,
        message: Annotated[str, Param("What to tell the user")],
        title: Annotated[str, Param("Short heading")] = "N.O.V.A.",
    ) -> str:
        presence = self.ctx.service("presence")
        if presence is not None:
            return await presence.reach_user(title, message)
        # No presence service: fall back to a plain on-screen panel.
        service = self.ctx.service("notifications", NotificationService)
        notification = Notification(title=title, body=message, source="assistant")
        if service is not None:
            await service.raise_notification(notification)
        else:
            self.ctx.bus.publish(Topics.NOTIFICATION, notification.as_payload(), source="assistant")
        return "Shown on screen."

    @tool("Turn do-not-disturb on or off.", mutating=True)
    async def set_do_not_disturb(
        self, enabled: Annotated[bool, Param("True to silence notifications")]
    ) -> str:
        self.ctx.store.patch({"notifications": {"do_not_disturb": enabled}})
        return f"Do not disturb {'on' if enabled else 'off'}."

    @tool("Change how fast N.O.V.A. speaks.", mutating=True)
    async def set_speech_speed(
        self, speed: Annotated[float, Param("Multiplier from 0.5 to 2.0; 1.0 is normal")]
    ) -> str:
        speed = max(0.5, min(2.0, speed))
        self.ctx.store.patch({"voice": {"tts": {"speed": speed}}})
        return f"Speaking at {speed:.2f} times normal speed."

    @tool("Change the voice N.O.V.A. speaks with.", mutating=True)
    async def set_voice(
        self, voice: Annotated[str, Param("Kokoro voice id, e.g. 'af_sarah'")]
    ) -> str:
        service = self.ctx.service("voice")
        available = service.synthesiser.available_voices() if service is not None else []
        if available and voice not in available:
            return f"I don't have that voice. Available: {', '.join(available[:12])}."
        self.ctx.store.patch({"voice": {"tts": {"voice": voice}}})
        return f"Switched to {voice}."

    @tool("Change the interface theme.", mutating=True)
    async def set_theme(
        self,
        theme: Annotated[
            Literal["nova-blue", "arc-cyan", "ember", "monochrome", "aurora"], Param("Theme name")
        ],
    ) -> str:
        self.ctx.store.patch({"appearance": {"theme": theme}})
        return f"Theme set to {theme.replace('-', ' ')}."

    @tool("Change the Core animation quality.", mutating=True)
    async def set_animation_quality(
        self,
        quality: Annotated[Literal["low", "balanced", "high", "ultra"], Param("Rendering quality")],
    ) -> str:
        self.ctx.store.patch({"appearance": {"animation_quality": quality}})
        return f"Animation quality set to {quality}."

    @tool("Stop speaking and return to idle.", mutating=True)
    async def stop_speaking(self) -> str:
        service = self.ctx.service("voice")
        if service is not None:
            await service.cancel()
        return "Stopped."
