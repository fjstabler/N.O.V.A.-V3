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
import contextlib
import secrets
import time
from enum import StrEnum
from typing import Any

from ..context import NovaContext
from ..runtime import NovaState, Service, Topics
from ..runtime.errors import DegradedCapability, NovaError
from .audio import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    AudioInput,
    AudioOutput,
    MicrophoneSource,
    list_devices,
)
from .remote import REMOTE_CAPTURE, RemoteMicrophone, RemoteSpeaker
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
        # The local pair is built once and kept, even on a machine with no
        # sound hardware: attaching a remote device swaps the pointers below,
        # and detaching has to have something to swap back to.
        self._local_input = AudioInput(
            device=settings.audio.input_device, gain=settings.audio.input_gain
        )
        self._local_output = AudioOutput(
            device=settings.audio.output_device, volume=settings.tts.volume
        )
        self.input: MicrophoneSource = self._local_input
        self.output: AudioOutput | RemoteSpeaker = self._local_output
        self._remote: RemoteMicrophone | None = None
        self._remote_session = ""
        self._local_microphone = False
        self._listen_task: asyncio.Task[None] | None = None
        self._level_task: asyncio.Task[None] | None = None
        self.wake = WakeWordDetector(
            settings.wake.model,
            sensitivity=settings.wake.sensitivity,
            cooldown=settings.wake.cooldown_seconds,
            consecutive_frames=settings.wake.consecutive_frames,
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
        self._frames_heard = 0
        self._last_hearing_report = 0.0
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
            self._local_input.start()
        except DegradedCapability as exc:
            # Not fatal, and on a headless box not even unexpected: a panel can
            # attach its own microphone later (see `attach_remote`), and until
            # one does, synthesis and typed input still work.
            self._degraded["microphone"] = exc.reason
            self.log.warning("microphone_unavailable", reason=exc.reason)
        else:
            self._local_microphone = True
            self._start_capture_loops()

        self.spawn(self._speech_worker(), name="voice-speech")
        self.ctx.store.on_change(self._on_settings_changed)

        if self._degraded:
            self.log.warning("voice_partially_degraded", **self._degraded)
        self.log.info("voice_ready", state=self._state.value, microphone=self._local_microphone)

    def _start_capture_loops(self) -> None:
        """Run the loops that only make sense with a microphone attached."""
        if self._listen_task is None:
            self._listen_task = self.spawn(self._listen_loop(), name="voice-listen")
        if self._level_task is None:
            self._level_task = self.spawn(self._level_reporter(), name="voice-levels")

    async def _stop_capture_loops(self) -> None:
        tasks = [task for task in (self._listen_task, self._level_task) if task is not None]
        self._listen_task = None
        self._level_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

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
        self._local_input.stop()  # a no-op if it was never opened, or is `input`
        self.transcriber.close()
        self.synthesiser.close()

    def describe(self) -> str:
        if self._degraded:
            return "degraded: " + ", ".join(f"{k} ({v})" for k, v in self._degraded.items())
        where = "remote mic" if self._remote is not None else "local mic"
        base = f"{self.transcriber.model_size} on {self.transcriber.resolved_device} · {where}"
        # A substituted wake phrase is the one piece of status nobody can guess
        # from the outside: the assistant is listening perfectly well, just not
        # for the words the settings page says it is.
        if self.wake.loaded and self.wake.active_model != self.wake.model_name:
            base += f' · saying "{_spoken(self.wake.active_model)}" wakes it'
        return base

    @property
    def wake_phrase(self) -> str:
        """The phrase that actually wakes it, spoken as a person would say it."""
        return _spoken(self.wake.active_model)

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "wakeWord": self.wake.loaded,
            "transcription": self.transcriber.loaded,
            "synthesis": self.synthesiser.loaded,
            "microphone": "microphone" not in self._degraded,
        }

    #: Settings that cannot be applied to a loaded model and need a restart.
    _RESTART_REQUIRED = ("voice.stt.model_size", "voice.stt.device", "voice.stt.compute_type")

    def _on_settings_changed(self, settings: Any, changed: dict[str, Any]) -> None:
        # Changing the wake phrase means loading a different model. Without this
        # the panel appears to accept the change and the detector carries on
        # with whatever it had — which reads as "the wake word just doesn't work".
        if "voice.wake.model" in changed or "voice.wake.enabled" in changed:
            self.spawn(self._reload_wake_word(), name="voice-wake-reload")

        for path in self._RESTART_REQUIRED:
            if path in changed:
                self._notify(
                    "warning",
                    "Restart required",
                    f"{path.rsplit('.', 1)[-1].replace('_', ' ')} takes effect "
                    "the next time N.O.V.A. starts.",
                )

        if (
            "voice.wake.sensitivity" in changed
            or "voice.wake.cooldown_seconds" in changed
            or "voice.wake.consecutive_frames" in changed
        ):
            self.wake.update(
                sensitivity=settings.voice.wake.sensitivity,
                cooldown=settings.voice.wake.cooldown_seconds,
                consecutive_frames=settings.voice.wake.consecutive_frames,
            )
        if "voice.tts.voice" in changed or "voice.tts.speed" in changed:
            self.synthesiser.update(voice=settings.voice.tts.voice, speed=settings.voice.tts.speed)
        if "voice.tts.volume" in changed:
            self.output.volume = settings.voice.tts.volume
        if "voice.audio.input_gain" in changed:
            self.input.gain = settings.voice.audio.input_gain

    async def _reload_wake_word(self) -> None:
        """Swap in a different wake word model without restarting."""
        settings = self.ctx.settings.voice.wake

        if not settings.enabled:
            self.wake.unload()
            self._degraded["wake word"] = "disabled"
            self.log.info("wake_word_disabled")
            return

        detector = WakeWordDetector(
            settings.model,
            sensitivity=settings.sensitivity,
            cooldown=settings.cooldown_seconds,
            consecutive_frames=settings.consecutive_frames,
            models_dir=self.ctx.paths.models_dir,
        )
        try:
            await detector.load()
        except DegradedCapability as exc:
            self._degraded["wake word"] = exc.reason
            self.log.warning("wake_word_reload_failed", reason=exc.reason, remedy=exc.remedy)
            self._notify("warning", "Wake word unavailable", exc.reason)
            return
        except Exception as exc:
            self._degraded["wake word"] = str(exc)
            self.log.exception("wake_word_reload_failed")
            self._notify("warning", "Wake word unavailable", str(exc)[:160])
            return

        self.wake = detector
        self._degraded.pop("wake word", None)
        self.log.info("wake_word_reloaded", model=settings.model)
        self._notify("success", "Wake word ready", f"Listening for '{settings.phrase}'.")

    def _notify(self, level: str, title: str, body: str) -> None:
        self.bus.publish(
            Topics.NOTIFICATION,
            {"level": level, "title": title, "body": body, "source": "voice", "timeout": 8.0},
            source=self.name,
        )

    # ------------------------------------------------------------- listen loop

    #: How often to report what the wake detector is hearing.
    _HEARING_REPORT_SECONDS = 20.0

    async def _listen_loop(self) -> None:
        async for frame in self.input.frames():
            if not frame:
                continue  # idle tick from the capture-queue timeout
            self._note_frame_heard()
            try:
                await self._on_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Whatever went wrong, the assistant must not be left mid-capture
                # or stuck in THINKING with no way back.
                self.log.exception("frame_processing_failed")
                self._reset_capture()
                await self._return_to_idle()

    def _note_frame_heard(self) -> None:
        """Periodically say what the microphone is actually delivering.

        "The wake word does not work" has three completely different causes and
        no way to tell them apart from outside: no audio arriving at all, audio
        arriving but silent, or audio that is fine and simply never scores high
        enough. Each needs a different fix and they are indistinguishable
        without numbers, so this prints them rather than leaving anyone to
        guess — which is otherwise a long conversation with a device that has
        no console.
        """
        self._frames_heard += 1
        now = time.monotonic()
        if self._last_hearing_report == 0.0:
            # First frame: start the clock rather than reporting a window that
            # has not happened yet.
            self._last_hearing_report = now
            return
        if now - self._last_hearing_report < self._HEARING_REPORT_SECONDS:
            return
        self._last_hearing_report = now
        frames, self._frames_heard = self._frames_heard, 0

        # A diagnostic must never be the thing that stops the microphone. This
        # runs on every frame in the listening path, so anything it touches —
        # including a wake detector swapped out for a stub — has to be unable
        # to break the loop it exists to explain.
        with contextlib.suppress(Exception):
            self.log.info(
                "wake_listening",
                frames=frames,
                peak_wake_score=round(self.wake.peak_score, 3),
                threshold=round(1.0 - self.ctx.settings.voice.wake.sensitivity, 3),
                listening_for=self.wake.active_model,
                source="remote" if self._remote is not None else "local",
            )
            self.wake.reset_peak()

    async def _on_frame(self, frame: bytes) -> None:
        if self._state is ListenState.SUSPENDED:
            return

        if self._state is ListenState.WAKE:
            if self.wake.loaded and self.wake.detected(frame):
                if self.ctx.state.state is not NovaState.IDLE:
                    # ListenState returns to WAKE the instant endpointing ends
                    # (_finish_capture, below) so the *next* utterance can be
                    # captured promptly — but transcription, reasoning and
                    # speaking for the utterance just captured are usually
                    # still in flight at that point, often for longer than the
                    # wake detector's own cooldown. A stray retrigger during
                    # that window (room echo, the tail of the same sentence)
                    # used to start a second capture that raced the first
                    # turn's state transitions and, if it ever produced a
                    # transcript, got Orchestrator.handle() to cancel the
                    # first turn outright before it could call a tool.
                    # Gating on NovaState rather than resetting the detector's
                    # own cooldown covers the whole turn, not just capture.
                    self.log.info("wake_ignored_turn_in_flight", state=self.ctx.state.state.value)
                    return
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

        if transcript.error:
            # Surface it. Sitting in THINKING with nothing on screen is the
            # worst possible way to report a failure.
            self.bus.publish(
                Topics.NOTIFICATION,
                {
                    "level": "warning",
                    "title": "Could not transcribe that",
                    "body": transcript.error,
                    "icon": "alert",
                    "source": "voice",
                    "timeout": 10.0,
                },
                source=self.name,
            )
            await self._return_to_idle()
            return

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

    # ------------------------------------------------------------ remote audio

    @property
    def remote_attached(self) -> bool:
        return self._remote is not None

    async def attach_remote(self, *, gain: float | None = None) -> dict[str, Any]:
        """Hand microphone and speaker duty to a device on the network.

        Everything downstream of the stream is untouched — the same detector
        scores the same frames with the same sensitivity — so a panel that
        attaches here gets the wake word and the follow-up window, not a
        second-class push-to-talk mode.
        """
        settings = self.ctx.settings.voice
        if not settings.audio.allow_remote:
            raise NovaError("remote audio is disabled in settings")

        if self._remote is not None:
            # A panel that reconnected without detaching cleanly. The newest
            # attach wins; the previous session is gone by definition.
            await self.detach_remote(reason="replaced")

        await self._stop_capture_loops()
        if self._local_microphone:
            self._local_input.stop()

        microphone = RemoteMicrophone(gain=settings.audio.input_gain if gain is None else gain)
        microphone.start()
        self._remote = microphone
        self._remote_session = secrets.token_urlsafe(9)
        self.input = microphone
        self.output = RemoteSpeaker(self._publish_remote, volume=settings.tts.volume)
        self._reset_capture()
        self._degraded.pop("microphone", None)
        self._start_capture_loops()
        self.spawn(self._remote_watchdog(self._remote_session), name="voice-remote-watch")

        self.log.info("remote_audio_attached", session=self._remote_session)
        return {
            "sessionId": self._remote_session,
            "sampleRate": SAMPLE_RATE,
            "frameSamples": FRAME_SAMPLES,
            "frameBytes": FRAME_BYTES,
            "capabilities": self.capabilities,
            "wakePhrase": settings.wake.phrase,
        }

    async def detach_remote(self, *, reason: str = "detached") -> bool:
        """Give the local hardware its job back, if this machine has any."""
        if self._remote is None:
            return False
        await self._stop_capture_loops()
        self._remote.stop()
        self._remote = None
        self._remote_session = ""
        self.input = self._local_input
        self.output = self._local_output
        self._reset_capture()

        try:
            self._local_input.start()
        except DegradedCapability as exc:
            self._local_microphone = False
            self._degraded["microphone"] = exc.reason
        else:
            self._local_microphone = True
            self._degraded.pop("microphone", None)
            self._start_capture_loops()

        await self._return_to_idle()
        self.log.info("remote_audio_detached", reason=reason, local=self._local_microphone)
        return True

    def submit_remote_frame(self, session: str, pcm: bytes) -> int:
        """Feed one message of microphone audio in from the attached device."""
        if self._remote is None:
            raise NovaError("no remote audio source is attached")
        if session and session != self._remote_session:
            # A panel that reconnected mid-flight, still draining its old
            # buffer. Refusing is what tells it to re-attach.
            raise NovaError("stale audio session")
        return self._remote.submit(pcm)

    async def _remote_watchdog(self, session: str) -> None:
        """Detach a device that stopped sending without saying goodbye.

        A panel that loses power or Wi-Fi never sends a detach, and without
        this the core would sit holding a microphone that will never produce
        another frame — deaf, but reporting itself healthy.
        """
        while self._remote_session == session:
            await asyncio.sleep(2.0)
            remote = self._remote
            if self._remote_session != session or remote is None:
                return
            if remote.stale:
                self.log.warning(
                    "remote_audio_stale", session=session, seconds=round(remote.seconds_since_frame)
                )
                await self.detach_remote(reason="stale")
                return

    def _publish_remote(self, topic: str, payload: dict[str, Any]) -> None:
        self.bus.publish(topic, {**payload, "sessionId": self._remote_session}, source=self.name)

    def _set_capture(self, capture: bool) -> None:
        """Gate the microphone while N.O.V.A. is speaking.

        Muting server-side is what makes it correct — frames are dropped
        before any consumer sees them. Telling the device as well stops it
        pushing audio up a link only to be discarded, and stops it capturing
        our own voice at the one place echo can actually be prevented.
        """
        self.input.set_muted(not capture)
        if self._remote is not None:
            self._publish_remote(REMOTE_CAPTURE, {"capture": capture})

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
            self._set_capture(False)

        await self.ctx.state.transition(NovaState.SPEAKING, reason="tts")
        self.bus.publish(Topics.SPEECH_STARTED, {"text": text}, source=self.name)
        try:
            await self.output.play(samples, sample_rate)
        finally:
            if muted:
                self._set_capture(True)
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
            "remote": self._remote is not None,
            "microphoneSource": "remote" if self._remote is not None else "local",
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


def _spoken(model_name: str) -> str:
    """Turn a model name into the words someone would actually say.

    `hey_jarvis` is not a phrase anyone speaks, and a status line that prints it
    verbatim asks the reader to work out the translation themselves. A path to a
    trained model keeps its stem for the same reason.
    """
    stem = model_name.rsplit("/", 1)[-1]
    if stem.endswith(".onnx"):
        stem = stem[: -len(".onnx")]
    return stem.replace("_", " ").strip()
