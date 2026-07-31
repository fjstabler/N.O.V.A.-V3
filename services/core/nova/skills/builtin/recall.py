"""Memory: what N.O.V.A. knows about the user and their world.

Recall happens automatically before every turn, so these tools exist for the
cases automatic retrieval cannot cover — deliberately committing something to
memory, correcting a stored fact, or answering "what do you know about X".
"""

from __future__ import annotations

from typing import Annotated, Literal

from ...memory.models import Entity, MemoryKind
from ...memory.service import MemoryService
from ..base import Param, Skill, tool


class MemorySkill(Skill):
    name = "memory"
    description = "Remember facts and preferences about the user, and recall them later."
    category = "Memory"
    prompt_hint = (
        "Store anything the user tells you about themselves, their home, their devices or how "
        "they like things done, using the memory tools. Do it silently — never announce it."
    )

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.memory.enabled:
            return False, "memory disabled in settings"
        if self.ctx.service("memory") is None:
            return False, "memory service is not running"
        return True, ""

    @property
    def memory(self) -> MemoryService:
        return self.ctx.require("memory", MemoryService)

    @tool("Store a durable fact or preference about the user, their home or their systems.")
    async def remember(
        self,
        content: Annotated[str, Param("The fact, written as a complete sentence")],
        kind: Annotated[
            Literal["fact", "preference", "event", "note"], Param("What sort of memory this is")
        ] = "fact",
        subject: Annotated[str, Param("What it is about", examples=("lighting", "the NAS"))] = "",
        importance: Annotated[float, Param("0 to 1; higher survives pruning")] = 0.6,
    ) -> str:
        await self.memory.remember(
            content,
            kind=MemoryKind(kind),
            subject=subject,
            importance=importance,
            source="explicit",
        )
        return "Noted."

    @tool("Search memory for what is known about a topic.")
    async def recall(
        self,
        query: Annotated[str, Param("What to look for")],
        limit: Annotated[int, Param("How many memories to return")] = 6,
    ) -> str:
        memories = await self.memory.recall(query, limit=max(1, min(limit, 20)))
        if not memories:
            return f"I don't have anything stored about '{query}'."
        return "\n".join(m.to_prompt_line() for m in memories)

    @tool("Forget everything stored about a topic.", destructive=True)
    async def forget(self, topic: Annotated[str, Param("What should be forgotten")]) -> str:
        removed = await self.memory.forget_matching(topic)
        return f"Forgot {removed} memor{'y' if removed == 1 else 'ies'} about '{topic}'."

    @tool("Set a named user preference, such as a default room or a preferred unit.")
    async def set_preference(
        self,
        key: Annotated[str, Param("Preference name", examples=("default_room", "news_source"))],
        value: Annotated[str, Param("Value to store")],
    ) -> str:
        await self.memory.set_preference(key, value)
        return f"Set {key.replace('_', ' ')} to {value}."

    @tool("Get a stored user preference.")
    async def get_preference(self, key: Annotated[str, Param("Preference name")]) -> str:
        value = await self.memory.get_preference(key)
        return f"{key}: {value}" if value is not None else f"No preference stored for '{key}'."

    @tool("List everything currently remembered, optionally filtered by type.")
    async def list_memories(
        self,
        kind: Annotated[
            Literal["all", "fact", "preference", "event", "entity", "note"], Param("Filter")
        ] = "all",
        limit: Annotated[int, Param("How many to list")] = 20,
    ) -> str:
        filter_kind = None if kind == "all" else MemoryKind(kind)
        memories = await self.memory.list_memories(filter_kind, limit=max(1, min(limit, 60)))
        if not memories:
            return "Memory is empty."
        return "\n".join(m.to_prompt_line() for m in memories)

    @tool("Record a person, room, device or service so it can be referred to by name.")
    async def remember_entity(
        self,
        name: Annotated[str, Param("What it is called")],
        kind: Annotated[str, Param("Type", examples=("person", "room", "device", "host"))],
        detail: Annotated[str, Param("Anything worth knowing about it")] = "",
        aliases: Annotated[str, Param("Other names, comma separated")] = "",
    ) -> str:
        await self.memory.upsert_entity(
            Entity(
                kind=kind,
                name=name,
                aliases=[a.strip() for a in aliases.split(",") if a.strip()],
                attributes={"detail": detail} if detail else {},
            )
        )
        return f"Recorded {name}."

    @tool("Look up people, rooms, devices or hosts by name.")
    async def find_entity(
        self,
        name: Annotated[str, Param("Name or partial name")],
        kind: Annotated[str, Param("Optional type filter")] = "",
    ) -> str:
        entities = await self.memory.find_entities(name, kind=kind or None)
        if not entities:
            return f"Nothing known called '{name}'."
        return "\n".join(
            f"{e.name} ({e.kind})"
            + (f": {e.attributes['detail']}" if e.attributes.get("detail") else "")
            for e in entities
        )

    @tool("Report how much N.O.V.A. currently remembers.")
    async def memory_stats(self) -> str:
        stats = await self.memory.stats()
        mode = "semantic and lexical" if stats.get("semantic") else "lexical"
        return (
            f"{stats['memories']} memories, {stats['entities']} entities, "
            f"{stats['preferences']} preferences. Recall is {mode}."
        )

    @tool("Start a fresh conversation, clearing the short-term context.", mutating=True)
    async def new_conversation(self) -> str:
        self.memory.new_conversation()
        return "Starting fresh."
