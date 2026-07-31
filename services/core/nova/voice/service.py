"""The voice service: one loop from microphone to spoken reply.

    microphone → wake word → endpointer → Whisper → orchestrator → Kokoro → speaker

Everything except the reasoning step is local. The loop is a small state machine
over a single audio stream:

``LISTENING_FOR_WAKE``  score every frame, do nothing else
``CAPTURING``           accumulate frames until the endpointer says stop
``FOLLOW_UP``           capture without a wake word, briefly, after N.O.V.A. speaks

The follow-up window is what makes conversation feel continuous — you say "Hey
Nova, what's the CPU at?" and then just "and the GPU?" without the wake word.

Each stage degrades independently: no wake model still allows manual activation
from the UI, no Whisper still allows typed input, no Kokoro still shows replies
on screen. A missing optional dependency narrows what N.O.V.A. can do; it never
stops it from running.
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import Any

from ..context import NovaContext
from ..runtime import NovaState, Service, Topics
from ..runtime.errors import DegradedCapability
from .audio import FRAME_SAMPLES, SAMPLE_RATE, AudioInput, AudioOutput, list_devices
from .stt import Transcriber
from .tts import Synthesiser
from .vad import Endpointer, EndpointState
from .wake import WakeWordDetector


class ListenState(StrEnum):
    WAKE = "wake"
    CAPTURING = "capturing"
    FOLLOW_UP = "follow_up"
    SUSPENDED = "suspended"


class VoiceService(Service):
    """Owns the microphone, the wake word, transcription and synthesis."""

    name = "voice"
    requires = ("orchestrator",)

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        settings = ctx.settings.voice
        self.input = AudioInput(device=settings.audio.input_device, gain=settings.audio.input_gain)
        self.output = AudioOutput(device=settings.audio.output_device, volume=settings.tts.volume)
        self.wake = WakeWordDetector(
            settings.wake.model,
            sensitivity=settings.wake.sensitivity,
            cooldown=settings.wake.cooldown_seconds,
            models_dir=ctx.paths.models_dir,
        )
        self.transcriber = Transcriber(
            model_size=settings.stt.model_size,
            device=settings.stt.device,
            compute_type=settings.stt.compute_type,
            language=settings.stt.language,
            beam_size=settings.stt.beam_size,
            models_dir=ctx.paths.models_dir,
        )
        self.synthesiser = Synthesiser(
            voice=settings.tts.voice, speed=settings.tts.speed, models_dir=ctx.paths.models_dir
        )
        self._state = ListenState.WAKE
        self._buffer = bytearray()
        self._endpointer: Endpointer | None = None
        self._speech_queue: asyncio.Queue[str] = asyncio.Queue()
        self._degraded: dict[str, str] = {}
        self._capture_started = 0.0

    # -------------------------------------------------------------- lifecycle

    async def on_start(self) -> None:
        settings = self.ctx.settings.voice
        if not settings.enabled:
            raise DegradedCapability("voice", "disabled in settings")

        # Load the stages independently so one missing model does not cost the rest.
        await self._load_stage("wake word", self.wake.load, enabled=settings.wake.enabled)
        await self._load_stage("transcription", self.transcriber.load)
        await self._load_stage("synthesis", self.synthesiser.load, enabled=settings.tts.enabled)

        try:
            self.input.start()
        except DegradedCapability as exc:
            self._degraded["microphone"] = exc.reason
            self.log.warning("microphone_unavailable", reason=exc.reason)
            # Synthesis may still work — keep the speech worker running.
            self.spawn(self._speech_worker(), name="voice-speech")
            return

        self.spawn(self._listen_loop(), name="voice-listen")
        self.spawn(self._speech_worker(), name="voice-speech")
        self.spawn(self._level_reporter(), name="voice-levels")
        self.ctx.store.on_change(self._on_settings_changed)

        if self._degraded:
            self.log.warning("voice_partially_degraded", **self._degraded)
        self.log.info("voice_ready", state=self._state.value)

    async def _load_stage(self, label: str, loader: Any, *, enabled: bool = True) -> None:
        if not enabled:
            self._degraded[label] = "disabled"
            return
        try:
            await loader()
        except DegradedCapability as exc:
            self._degraded[label] = exc.reason
            self.bus.publish(Topics.CAPABILITY_DEGRADED, exc.as_payload(), source=self.name)
            self.log.warning(
                "voice_stage_unavailable", stage=label, reason=exc.reason, remedy=exc.remedy
            )
        except Exception as exc:
            self._degraded[label] = str(exc)
            self.log.exception("voice_stage_failed", stage=label)

    async def on_stop(self) -> None:
        self.output.cancel()
        self.input.stop()
        self.transcriber.close()
        self.synthesiser.close()

    def describe(self) -> str:
        if self._degraded:
            return "degraded: " + ", ".join(f"{k} ({v})" for k, v in self._degraded.items())
        return f"{self.transcriber.model_size} on {self.transcriber.resolved_device}"

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "wakeWord": self.wake.loaded,
            "transcription": self.transcriber.loaded,
            "synthesis": self.synthesiser.loaded,
            "microphone": "microphone" not in self._degraded,
        }

    def _on_settings_changed(self, settings: Any, changed: dict[str, Any]) -> None:
        if "voice.wake.sensitivity" in changed or "voice.wake.cooldown_seconds" in changed:
            self.wake.update(
                sensitivity=settings.voice.wake.sensitivity,
                cooldown=settings.voice.wake.cooldown_seconds,
            )
        if "voice.tts.voice" in changed or "voice.tts.speed" in changed:
            self.synthesiser.update(voice=settings.voice.tts.voice, speed=settings.voice.tts.speed)
        if "voice.tts.volume" in changed:
            self.output.volume = settings.voice.tts.volume
        if "voice.audio.input_gain" in changed:
            self.input.gain = settings.voice.audio.input_gain

    # ------------------------------------------------------------- listen loop

    async def _listen_loop(self) -> None:
        async for frame in self.input.frames():
            if not frame:
                continue  # idle tick from the capture-queue timeout
            try:
                await self._on_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("frame_processing_failed")

    async def _on_frame(self, frame: bytes) -> None:
        if self._state is ListenState.SUSPENDED:
            return

        if self._state is ListenState.WAKE:
            if self.wake.loaded and self.wake.detected(frame):
                await self._begin_capture(triggered_by="wake")
            return

        # CAPTURING or FOLLOW_UP: accumulate frames and endpoint the utterance.
        self._buffer.extend(frame)
        assert self._endpointer is not None
        result = self._endpointer.feed(frame)

        if result.state is EndpointState.DONE:
            await self._finish_capture()
        elif result.state is EndpointState.TIMEOUT:
            await self._abandon_capture()

    async def _begin_capture(self, *, triggered_by: str) -> None:
        settings = self.ctx.settings.voice
        self._state = (
            ListenState.FOLLOW_UP if triggered_by == "follow-up" else ListenState.CAPTURING
        )
        self._capture_started = time.monotonic()
        self._endpointer = Endpointer(
            silence_ms=settings.stt.silence_ms,
            max_utterance_seconds=settings.stt.max_utterance_seconds,
            aggressiveness=settings.stt.vad_aggressiveness,
            start_timeout_seconds=6.0 if triggered_by != "follow-up" else settings.follow_up_window,
        )
        # Seed with pre-roll so the first word after the wake phrase survives.
        self._buffer = bytearray(self.input.read_preroll() if triggered_by == "wake" else b"")

        if triggered_by == "wake":
            self.bus.publish(
                Topics.WAKE_DETECTED, {"phrase": settings.wake.phrase}, source=self.name
            )
        self.bus.publish(Topics.LISTEN_STARTED, {"trigger": triggered_by}, source=self.name)
        await self.ctx.state.transition(NovaState.LISTENING, reason=triggered_by)

        if settings.wake.phrase and self.ctx.settings.assistant.wake_response:
            await self.speak(self.ctx.settings.assistant.wake_response)

    async def _finish_capture(self) -> None:
        audio = bytes(self._buffer)
        had_speech = self._endpointer is not None and self._endpointer.had_speech
        self._reset_capture()
        self.bus.publish(Topics.LISTEN_ENDED, {"bytes": len(audio)}, source=self.name)

        if not had_speech or len(audio) < FRAME_SAMPLES * 4:
            await self._return_to_idle()
            return

        if not self.transcriber.loaded:
            self.log.info("transcription_unavailable_discarding_audio")
            await self._return_to_idle()
            return

        await self.ctx.state.transition(NovaState.THINKING, reason="transcribing")
        transcript = await self.transcriber.transcribe(audio)
        self.log.info(
            "transcribed",
            text=transcript.text[:120],
            ms=transcript.duration_ms,
            audio_ms=transcript.audio_ms,
            confidence=transcript.confidence,
        )

        if not transcript.usable:
            await self._return_to_idle()
            return

        self.bus.publish(
            Topics.TRANSCRIPT_FINAL,
            {"text": transcript.text, "confidence": transcript.confidence, "source": "voice"},
            source=self.name,
        )

    async def _abandon_capture(self) -> None:
        self._reset_capture()
        self.bus.publish(Topics.LISTEN_ENDED, {"bytes": 0, "reason": "no-speech"}, source=self.name)
        await self._return_to_idle()

    def _reset_capture(self) -> None:
        self._state = ListenState.WAKE
        self._buffer = bytearray()
        self._endpointer = None

    async def _return_to_idle(self) -> None:
        if self.ctx.state.state in (NovaState.LISTENING, NovaState.THINKING):
            await self.ctx.state.transition(NovaState.IDLE, reason="nothing-heard")

    # ------------------------------------------------------------- activation

    async def activate(self) -> bool:
        """Start listening without the wake word (UI button, hotkey)."""
        if self._state in (ListenState.CAPTURING, ListenState.FOLLOW_UP):
            return False
        if "microphone" in self._degraded:
            return False
        await self._begin_capture(triggered_by="manual")
        return True

    async def cancel(self) -> None:
        """Abort listening or speaking immediately."""
        self.output.cancel()
        _drain_queue(self._speech_queue)
        if self._state in (ListenState.CAPTURING, ListenState.FOLLOW_UP):
            self._reset_capture()
        await self.ctx.state.transition(NovaState.IDLE, reason="cancelled")

    def suspend(self) -> None:
        self._state = ListenState.SUSPENDED

    def resume(self) -> None:
        if self._state is ListenState.SUSPENDED:
            self._state = ListenState.WAKE

    # ---------------------------------------------------------------- speaking

    async def speak(self, text: str) -> None:
        """Queue text for synthesis. Returns immediately."""
        if text.strip():
            await self._speech_queue.put(text)

    async def _speech_worker(self) -> None:
        """Serialises playback so sentences never overlap."""
        while True:
            text = await self._speech_queue.get()
            try:
                await self._speak_now(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("speech_failed")
            finally:
                self._speech_queue.task_done()

    async def _speak_now(self, text: str) -> None:
        settings = self.ctx.settings.voice
        if not self.synthesiser.loaded:
            # No synthesis: the UI still shows the reply, so just mark the beat.
            self.bus.publish(
                Topics.SPEECH_STARTED, {"text": text, "silent": True}, source=self.name
            )
            self.bus.publish(Topics.SPEECH_ENDED, {"silent": True}, source=self.name)
            return

        result = await self.synthesiser.synthesise(text)
        if result is None:
            return
        samples, sample_rate = result

        muted = settings.wake.mute_while_speaking
        if muted:
            # Without echo cancellation, our own output would re-trigger the wake word.
            self.input.set_muted(True)

        await self.ctx.state.transition(NovaState.SPEAKING, reason="tts")
        self.bus.publish(Topics.SPEECH_STARTED, {"text": text}, source=self.name)
        try:
            await self.output.play(samples, sample_rate)
        finally:
            if muted:
                self.input.set_muted(False)
                self.input.drain()
                self.wake.reset()
            self.bus.publish(Topics.SPEECH_ENDED, {}, source=self.name)
            await self._after_speaking()

    async def _after_speaking(self) -> None:
        """Open the follow-up window, or settle back to idle."""
        if not self._speech_queue.empty():
            return  # more sentences of the same reply still to play

        settings = self.ctx.settings.voice
        orchestrator = self.ctx.service("orchestrator")
        if orchestrator is not None and orchestrator.busy:
            return  # the turn is still producing text

        if settings.conversation_mode and settings.follow_up_window > 0 and self.transcriber.loaded:
            await self.ctx.state.transition(NovaState.IDLE, reason="awaiting-follow-up")
            await self._begin_capture(triggered_by="follow-up")
        else:
            await self.ctx.state.transition(NovaState.IDLE, reason="spoke")

    # ------------------------------------------------------------------ levels

    async def _level_reporter(self) -> None:
        """Publishes microphone loudness so the Core can react to the voice."""
        while True:
            await asyncio.sleep(1 / 30)
            level = self.input.level
            if level > 0.001 or self._capturing:
                self.bus.publish(
                    Topics.AUDIO_LEVEL,
                    {"level": round(level, 4), "listening": self._capturing},
                    source=self.name,
                )

    @property
    def _capturing(self) -> bool:
        return self._state in (ListenState.CAPTURING, ListenState.FOLLOW_UP)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "capabilities": self.capabilities,
            "degraded": dict(self._degraded),
            "voices": self.synthesiser.available_voices(),
            "sampleRate": SAMPLE_RATE,
            "sttDevice": self.transcriber.resolved_device,
        }

    @staticmethod
    def devices() -> list[dict[str, Any]]:
        return [d.as_payload() for d in list_devices()]


def _drain_queue(queue_: asyncio.Queue[Any]) -> None:
    while True:
        try:
            queue_.get_nowait()
            queue_.task_done()
        except (asyncio.QueueEmpty, ValueError):
            return
