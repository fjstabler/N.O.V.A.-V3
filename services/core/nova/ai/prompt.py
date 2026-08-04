"""System prompt assembly.

The prompt is rebuilt every turn from live state: the time, the host, what the
skills report, and whatever memory recall surfaced for this specific utterance.
Rebuilding beats caching here — a stale "it is 14:02" is worse than the handful
of tokens it costs to be right.

Two constraints shape the wording. Replies are spoken aloud, so they must be
short and free of markdown. And N.O.V.A. acts on real systems, so it must be
explicit about what it did rather than implying success.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from ..context import NovaContext
    from ..memory.models import Memory

_STYLE = """\
Response rules:
- You are speaking aloud. Keep replies to one or two short sentences unless asked for detail.
- Never use markdown, bullet points, emoji, or code fences. Write plain spoken prose.
- Read numbers naturally: "forty-two percent", "three point two gigabytes".
- Do not restate the question or open with filler like "Sure" or "Certainly".
- If you performed an action, say what happened in past tense, briefly.
- If a tool failed, say what failed and the likely reason. Never claim success you did not verify.
- If you do not know something and no tool can find it, say so plainly.
- When several things match a request, ask one short clarifying question instead of guessing.
"""

_SAFETY = """\
Acting on the user's systems:
- Prefer a read-only tool before a mutating one; check state before changing it.
- Destructive actions are confirmed with the user automatically. Describe the action plainly
  and wait — do not attempt to bypass or re-issue the call.
- Never invent entity ids, container names, file paths or unit names. Look them up first.
"""


def build_system_prompt(ctx: NovaContext, memories: list[Memory] | None = None) -> str:
    settings = ctx.settings
    sections: list[str] = [settings.assistant.persona.strip(), _STYLE]

    if ctx.skills is not None and ctx.skills.tools:
        sections.append(_SAFETY)

    sections.append(_environment_block(ctx))

    context_lines = ctx.skills.prompt_context() if ctx.skills is not None else []
    if context_lines:
        sections.append("Current environment:\n" + "\n".join(f"- {line}" for line in context_lines))

    if memories:
        sections.append(_memory_block(memories))

    return "\n\n".join(s for s in sections if s.strip())


def _memory_block(memories: list[Memory]) -> str:
    """Render recalled memories, weighted by how much they deserve to be trusted.

    Curated facts (stored via the explicit `remember` tool, ``source="explicit"``)
    are durable and safe to state as true. Auto-logged conversation exchanges
    (``source="turn"``, written after every turn regardless of what was said) are
    only a record of what was once said — including a capability refusal like "I
    don't have access to Home Assistant" made before it was ever connected.
    Presenting both under one "remember this as fact" heading is what let a
    stale refusal keep getting repeated after the integration came online: the
    excerpt read exactly as authoritative as a real fact, so the model trusted it
    over the live tool list below it. Splitting them, and telling the model the
    excerpts may be stale, lets the current environment/tools always win when the
    two disagree.
    """
    facts = [m for m in memories if m.source != "turn"]
    recent = [m for m in memories if m.source == "turn"]
    blocks: list[str] = []
    if facts:
        blocks.append(
            "Relevant things you remember about this user "
            "(use them naturally; do not announce that you remembered):\n"
            + "\n".join(f"- {m.to_prompt_line()}" for m in facts)
        )
    if recent:
        blocks.append(
            "Excerpts from earlier conversations (context only, not verified facts — they "
            "may be outdated, especially about what you can or cannot do. If one conflicts "
            "with your current tools or the environment described above, trust the current "
            "state, not the excerpt):\n" + "\n".join(f"- {m.to_prompt_line()}" for m in recent)
        )
    return "\n\n".join(blocks)


def _environment_block(ctx: NovaContext) -> str:
    settings = ctx.settings
    now = _now(settings.assistant.timezone)
    lines = [
        f"Current date and time: {now.strftime('%A %d %B %Y, %H:%M')} ({now.tzname() or 'local'}).",
        f"Units: {settings.assistant.units}.",
    ]
    if settings.assistant.location:
        lines.append(f"User location: {settings.assistant.location}.")

    system = ctx.services.get("system")
    if system is not None and getattr(system, "host", None) is not None:
        host = system.host
        lines.append(
            f"Primary host: {host.hostname} ({host.platform} {host.release}, "
            f"{host.logical_cores} cores, {host.total_memory_gb:.0f} GB RAM)."
        )
    return "\n".join(lines)


def _now(timezone_name: str) -> datetime:
    if timezone_name:
        try:
            return datetime.now(ZoneInfo(timezone_name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone()


def build_messages(
    system_prompt: str,
    history: list[tuple[str, str]],
    user_text: str,
) -> list[dict[str, str]]:
    """Assemble the chat message list for one turn."""
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        if role in ("user", "assistant") and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


def summarise_for_memory(user_text: str, reply: str) -> str:
    """Compact a turn into one line for long-term storage."""
    user_text = " ".join(user_text.split())[:300]
    reply = " ".join(reply.split())[:300]
    return f"User asked: {user_text} — N.O.V.A. replied: {reply}"


def greeting_for_time(now: float | None = None) -> str:
    hour = datetime.fromtimestamp(now or time.time()).hour
    if hour < 5:
        return "You're up late."
    if hour < 12:
        return "Good morning."
    if hour < 18:
        return "Good afternoon."
    return "Good evening."
