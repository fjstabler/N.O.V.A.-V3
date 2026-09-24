"""Orchestrator: which turns get spoken through the local speaker.

A mobile/remote client has no speaker of its own worth announcing to at
home — it reads its own reply aloud on the device that asked. Voice and
typed-text turns are unchanged: both still speak locally, exactly as before.
"""

from __future__ import annotations

from nova.ai.orchestrator import Orchestrator
from nova.context import NovaContext


class FakeVoice:
    name = "voice"
    running = True

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, text: str) -> None:
        self.spoken.append(text)


def make_orchestrator(ctx: NovaContext) -> tuple[Orchestrator, FakeVoice]:
    orchestrator = Orchestrator(ctx)
    voice = FakeVoice()
    ctx.services.register(voice)  # type: ignore[arg-type]
    return orchestrator, voice


async def test_mobile_turns_are_not_spoken_locally(ctx: NovaContext) -> None:
    orchestrator, voice = make_orchestrator(ctx)
    orchestrator._current_source = "mobile"

    await orchestrator._synthesise("the kitchen light is on")

    assert voice.spoken == []


async def test_voice_turns_are_still_spoken_locally(ctx: NovaContext) -> None:
    orchestrator, voice = make_orchestrator(ctx)
    orchestrator._current_source = "voice"

    await orchestrator._synthesise("the kitchen light is on")

    assert voice.spoken == ["the kitchen light is on"]


async def test_typed_text_turns_are_still_spoken_locally(ctx: NovaContext) -> None:
    """Existing behaviour, unchanged: only "mobile" is special-cased."""
    orchestrator, voice = make_orchestrator(ctx)
    orchestrator._current_source = "text"

    await orchestrator._synthesise("the kitchen light is on")

    assert voice.spoken == ["the kitchen light is on"]


async def test_a_full_mobile_turn_never_reaches_the_local_speaker(ctx: NovaContext) -> None:
    """Runs the real handle() -> _run_turn() -> _fail() -> _synthesise() path
    (via the "no API key configured" error, which still tries to speak the
    failure message) to prove the source is set before synthesis can happen,
    not just when _synthesise is called directly."""
    orchestrator, voice = make_orchestrator(ctx)

    result = await orchestrator.handle("what's on my calendar", source="mobile")

    assert result.error
    assert voice.spoken == []


async def test_a_full_voice_turn_still_speaks_its_failure_locally(ctx: NovaContext) -> None:
    orchestrator, voice = make_orchestrator(ctx)

    result = await orchestrator.handle("what's on my calendar", source="voice")

    assert result.error
    assert voice.spoken == [result.error]
