"""System prompt assembly, in particular how recalled memory is weighted."""

from __future__ import annotations

from nova.ai.prompt import build_system_prompt
from nova.context import NovaContext
from nova.memory.models import Memory


def test_no_memory_section_when_nothing_was_recalled(ctx: NovaContext) -> None:
    prompt = build_system_prompt(ctx, [])
    assert "remember" not in prompt.lower()
    assert "earlier conversations" not in prompt.lower()


def test_explicit_facts_are_presented_as_trusted(ctx: NovaContext) -> None:
    fact = Memory(
        content="Prefers the kitchen lights warm white",
        subject="lighting",
        source="explicit",
    )
    prompt = build_system_prompt(ctx, [fact])
    assert "Relevant things you remember about this user" in prompt
    assert "warm white" in prompt
    assert "Excerpts from earlier conversations" not in prompt


def test_auto_logged_turns_are_flagged_as_possibly_stale(ctx: NovaContext) -> None:
    """The regression this guards: a capability refusal recorded before Home
    Assistant was connected must not be handed to the model as settled fact
    once the integration is live — the model has to be told the excerpt may
    be outdated and the live tool list wins if they disagree."""
    stale = Memory(
        content=(
            "User asked: what lights are on — N.O.V.A. replied: I don't have "
            "access to your home assistant or lighting system."
        ),
        subject="conversation",
        source="turn",
    )
    prompt = build_system_prompt(ctx, [stale])
    assert "Excerpts from earlier conversations" in prompt
    assert "may be outdated" in prompt
    assert "trust the current state, not the excerpt" in prompt
    assert "Relevant things you remember about this user" not in prompt
    assert "I don't have access to your home assistant" in prompt


def test_facts_and_recent_turns_are_split_into_separate_blocks(ctx: NovaContext) -> None:
    fact = Memory(content="Lives in the garage", subject="NAS", source="explicit")
    turn = Memory(
        content="User asked: is the NAS online — N.O.V.A. replied: yes, it's reachable.",
        subject="conversation",
        source="turn",
    )
    prompt = build_system_prompt(ctx, [fact, turn])
    facts_heading = prompt.index("Relevant things you remember about this user")
    turns_heading = prompt.index("Excerpts from earlier conversations")
    assert facts_heading < turns_heading
    assert "Lives in the garage" in prompt
    assert "is the NAS online" in prompt
