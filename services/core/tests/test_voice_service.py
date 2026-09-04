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

from typing import Any

from nova.context import NovaContext
from nova.runtime import NovaState, Topics
from nova.voice.audio import FRAME_SAMPLES
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


# ------------------------------------------------------ wake settings wiring


async def test_the_real_detector_is_built_with_the_configured_consecutive_frames(
    ctx: NovaContext,
) -> None:
    ctx.store.patch({"voice": {"wake": {"consecutive_frames": 5}}}, persist=False)
    voice = VoiceService(ctx)
    assert voice.wake.consecutive_frames == 5


async def test_changing_consecutive_frames_updates_the_live_detector(
    ctx: NovaContext,
) -> None:
    """Regression: this setting was added alongside sensitivity and cooldown,
    which already apply live via `_on_settings_changed` — without wiring it
    through the same way, changing it in Settings would silently do nothing
    until a full restart, exactly the "the panel appears to accept the
    change" trap `_on_settings_changed`'s own comment already warns about
    for the wake model itself."""
    voice = VoiceService(ctx)
    assert voice.wake.consecutive_frames != 6

    settings = ctx.store.patch({"voice": {"wake": {"consecutive_frames": 6}}}, persist=False)
    voice._on_settings_changed(settings, {"voice.wake.consecutive_frames": 6})

    assert voice.wake.consecutive_frames == 6


async def test_it_says_so_rather_than_discarding_speech_it_cannot_transcribe(
    ctx: NovaContext,
) -> None:
    """Waking, listening and then going quiet is indistinguishable from being
    ignored. Someone whose Whisper model had not loaded got exactly that: the
    wake word fired, the Core lit up, and every sentence after it vanished
    without a word."""
    voice = VoiceService(ctx)
    voice._degraded["transcription"] = "model download failed"
    assert not voice.transcriber.loaded

    notices: list[dict[str, Any]] = []
    ctx.bus.subscribe(Topics.NOTIFICATION, lambda event: notices.append(event.payload))

    voice._buffer.extend(b"\x01\x02" * FRAME_SAMPLES * 8)
    voice._endpointer = _SpeechHeard()  # type: ignore[assignment]

    await voice._finish_capture()

    assert notices, "the user has to be told, not just the log"
    assert "model download failed" in notices[0]["body"]
    assert not voice._speech_queue.empty(), "and told aloud, where there is a voice"


class _SpeechHeard:
    """An endpointer that reports it captured speech."""

    had_speech = True
