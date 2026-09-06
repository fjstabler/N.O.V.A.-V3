"""Memory value objects."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MemoryKind(StrEnum):
    FACT = "fact"
    """Something true about the world or the user's setup."""
    PREFERENCE = "preference"
    """How the user likes things done."""
    EVENT = "event"
    """Something that happened, with a timestamp."""
    ENTITY = "entity"
    """A device, room, person or service."""
    SUMMARY = "summary"
    """A condensed conversation."""
    NOTE = "note"
    """Free-form, user-dictated."""


@dataclass(slots=True)
class Memory:
    content: str
    kind: MemoryKind = MemoryKind.FACT
    subject: str = ""
    importance: float = 0.5
    source: str = "conversation"
    metadata: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    accessed_at: float = 0.0
    access_count: int = 0
    expires_at: float | None = None
    score: float = 0.0

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "subject": self.subject,
            "content": self.content,
            "importance": round(self.importance, 3),
            "source": self.source,
            "createdAt": self.created_at,
            "score": round(self.score, 4),
            "metadata": self.metadata,
        }

    def to_prompt_line(self) -> str:
        prefix = f"[{self.kind.value}]"
        if self.subject:
            prefix += f" {self.subject}:"
        return f"{prefix} {self.content}".strip()


@dataclass(slots=True)
class Turn:
    role: str  # "user" | "assistant" | "tool"
    content: str
    id: int | None = None
    conversation_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Entity:
    """A thing N.O.V.A. can refer to by name: a light, a room, a person, a host."""

    kind: str
    name: str
    aliases: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    updated_at: float = field(default_factory=time.time)

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "aliases": self.aliases,
            "attributes": self.attributes,
        }
