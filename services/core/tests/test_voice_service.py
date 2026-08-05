"""Voice service: the wake word must not re-arm mid-turn.

Regression: `_finish_capture()` returns `ListenState` to `WAKE` the instant
endpointing ends, so the mic can catch the *next* utterance promptly — but
transcription, reasoning and speaking for the utterance just captured are
still in flight at that point, often for longer than the wake detector's own
cooldown. A stray retrigger during that window (room echo, the tail of the
same sentence) started a second capture that raced the first turn's own
`NovaState` transitions and, if it ever produced a transcript,
`Orchestrator.handle()` would cancel the first turn outright before it could
call a tool — "Hey Nova, this is my face" would start reasoning and then just
vanish, no `tool_invoked`, no reply, no error.
"""

from __future__ import annotations

from nova.context import NovaContext
from nova.runtime import NovaState
from nova.voice.service import ListenState, VoiceService


class AlwaysDetects:
    """Stands in for a loaded WakeWordDetector that always fires."""

    loaded = True

    def detected(self, frame: bytes) -> bool:
        return True


def make_voice(ctx: NovaContext) -> VoiceService:
    voice = VoiceService(ctx)
    voice.wake = AlwaysDetects()  # type: ignore[assignment]
    return voice


async def test_a_wake_trigger_mid_turn_is_ignored_not_raced(ctx: NovaContext) -> None:
    voice = make_voice(ctx)
    await ctx.state.transition(NovaState.IDLE)
    await ctx.state.transition(NovaState.THINKING, reason="transcribing")

    await voice._on_frame(b"\x00\x00" * 80)

    # Still WAKE, not CAPTURING: no second capture was started...
    assert voice._state is ListenState.WAKE
    # ...and the first turn's own state was left alone, not yanked to LISTENING.
    assert ctx.state.state is NovaState.THINKING


async def test_a_wake_trigger_while_speaking_is_also_ignored(ctx: NovaContext) -> None:
    voice = make_voice(ctx)
    await ctx.state.transition(NovaState.IDLE)
    await ctx.state.transition(NovaState.THINKING, reason="transcribing")
    await ctx.state.transition(NovaState.SPEAKING, reason="tts")

    await voice._on_frame(b"\x00\x00" * 80)

    assert voice._state is ListenState.WAKE
    assert ctx.state.state is NovaState.SPEAKING


async def test_a_wake_trigger_while_genuinely_idle_still_starts_a_capture(
    ctx: NovaContext,
) -> None:
    """The gate only blocks mid-turn retriggers — a real wake word while
    nothing else is happening must still work."""
    voice = make_voice(ctx)
    await ctx.state.transition(NovaState.IDLE)

    await voice._on_frame(b"\x00\x00" * 80)

    assert voice._state is ListenState.CAPTURING
    assert ctx.state.state is NovaState.LISTENING
