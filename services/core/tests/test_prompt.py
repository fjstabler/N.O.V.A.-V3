"""System prompt assembly, in particular how recalled memory is weighted."""

from __future__ import annotations

from nova.ai.prompt import build_system_prompt, facts_for_prompt
from nova.context import NovaContext
from nova.memory.models import Memory


def test_the_style_rules_forbid_promising_without_calling_the_tool(ctx: NovaContext) -> None:
    """Regression: "Hey Nova, watch my room" got a spoken "I'll watch it" with
    no security_arm_room_watch call anywhere in the logs — the model just
    talked. The tool's own description already said this phrase means
    arm_room_watch "never anything else", so the gap was not a missing
    instruction on that one skill; it was that nothing told the model, in
    general, that a verbal promise has to be backed by an actual call. This
    pins the general rule down so any future skill gets it for free."""
    prompt = build_system_prompt(ctx, [])
    assert "no tool call behind it" in prompt


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


def test_facts_for_prompt_drops_auto_logged_turns() -> None:
    """The actual fix: even a caveated recalled turn measurably swayed a cheap
    model away from calling a tool, for two unrelated skills in a row. Turn
    exchanges are cut before build_system_prompt ever sees them, rather than
    trusting the wording of a caveat to be enough."""
    fact = Memory(content="Lives in the garage", subject="NAS", source="explicit")
    turn = Memory(
        content="User asked: what's on my calendar — N.O.V.A. replied: I can't access it.",
        subject="conversation",
        source="turn",
    )
    kept = facts_for_prompt([fact, turn])
    assert kept == [fact]


def test_facts_for_prompt_keeps_everything_else() -> None:
    explicit = Memory(content="Prefers metric", source="explicit")
    default_source = Memory(content="Some memory with the default source")
    kept = facts_for_prompt([explicit, default_source])
    assert kept == [explicit, default_source]
