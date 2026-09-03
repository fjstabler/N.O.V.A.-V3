"""A microphone and speaker that live on another device.

The point of this path is that nothing downstream of the stream can tell the
difference: the same detector scores the same frames, the same endpointer cuts
the same utterance. So these tests care most about the two places where a
network can change the shape of the data and quietly break that promise —
frames arriving in the wrong sizes, and a device that stops sending without
saying so.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import wave
from typing import Any

import numpy
import pytest

from nova.context import NovaContext
from nova.runtime import NovaState
from nova.runtime.errors import NovaError
from nova.voice.audio import FRAME_BYTES, FRAME_SAMPLES
from nova.voice.remote import (
    REMOTE_CAPTURE,
    REMOTE_PLAY,
    REMOTE_STOP,
    RemoteMicrophone,
    RemoteSpeaker,
)
from nova.voice.service import ListenState, VoiceService


def silence(frames: int = 1) -> bytes:
    return b"\x00\x00" * (FRAME_SAMPLES * frames)


def tone(frames: int = 1, amplitude: int = 8000) -> bytes:
    samples = numpy.full(FRAME_SAMPLES * frames, amplitude, dtype=numpy.int16)
    return samples.tobytes()


class AlwaysDetects:
    loaded = True

    def detected(self, frame: bytes) -> bool:
        return True

    def reset(self) -> None:
        pass


async def quiesce(voice: VoiceService) -> None:
    """Cancel the loops a bare VoiceService spawned.

    `Service.stop()` short-circuits on a service that was never started through
    the manager, so without this the listen and level loops outlive the test
    and surface later as 'task was destroyed but it is pending'.
    """
    tasks = tuple(voice._tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ------------------------------------------------------------- re-chunking


def test_one_exact_frame_arrives_as_one_frame() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    assert microphone.submit(silence()) == 1


def test_a_batched_send_is_cut_back_into_wake_word_frames() -> None:
    """A client is free to put four frames in one message to save round trips.
    openWakeWord is not free to score 5120 samples at once, so the split has to
    happen here rather than being pushed onto every client."""
    microphone = RemoteMicrophone()
    microphone.start()
    assert microphone.submit(silence(4)) == 4


def test_a_frame_split_across_two_messages_is_reassembled() -> None:
    """The other direction, and the one a naive implementation gets wrong: a
    partial frame must be held, not scored short and not dropped."""
    microphone = RemoteMicrophone()
    microphone.start()
    half = FRAME_BYTES // 2

    assert microphone.submit(silence()[:half]) == 0  # nothing whole yet
    assert microphone.submit(silence()[half:]) == 1  # completed by the second half


def test_a_ragged_stream_still_yields_whole_frames_and_keeps_the_remainder() -> None:
    """Sizes that line up with nothing — the realistic case once a device's own
    buffering is involved."""
    microphone = RemoteMicrophone()
    microphone.start()
    produced = sum(microphone.submit(silence(3)[: FRAME_BYTES + 101]) for _ in range(3))

    # 3 sends of 2661 bytes = 7983 bytes; 3 whole 2560-byte frames fit, and the
    # 303-byte tail is still held rather than being padded or discarded.
    assert produced == 3
    assert len(microphone._pending) == 7983 - (3 * FRAME_BYTES)


def test_frames_reach_a_consumer_in_the_size_the_detector_expects() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.submit(silence(3))

    assert microphone._queue.qsize() == 3
    assert all(len(microphone._queue.get_nowait()) == FRAME_BYTES for _ in range(3))


def test_an_empty_send_is_harmless() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    assert microphone.submit(b"") == 0


def test_a_closed_microphone_accepts_nothing_further() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.stop()
    assert microphone.submit(silence()) == 0


# ---------------------------------------------------------------- behaviour


def test_gain_is_applied_before_a_consumer_sees_the_frame() -> None:
    microphone = RemoteMicrophone(gain=2.0)
    microphone.start()
    microphone.submit(tone(amplitude=1000))

    frame = numpy.frombuffer(microphone._queue.get_nowait(), dtype=numpy.int16)
    assert int(frame[0]) == 2000


def test_muted_frames_are_withheld_from_consumers_but_still_fill_the_preroll() -> None:
    """Matching the local microphone exactly: muting stops N.O.V.A. hearing its
    own voice, but the pre-roll has to keep running or the first word after an
    unmute is the one that goes missing."""
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.set_muted(True)
    microphone.submit(silence(2))

    assert microphone._queue.qsize() == 0
    assert len(microphone.read_preroll()) == 2 * FRAME_BYTES


def test_the_preroll_is_bounded_rather_than_growing_without_limit() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.submit(silence(100))

    assert len(microphone.read_preroll()) <= FRAME_BYTES * microphone._preroll.maxlen


def test_a_backed_up_consumer_loses_frames_rather_than_stalling_the_socket() -> None:
    """Dropping is the deliberate choice. The producer is a WebSocket, and
    back-pressuring it would queue audio in the kernel — turning 'we missed a
    moment' into 'everything you say arrives late, forever'."""
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.submit(silence(RemoteMicrophone.QUEUE_FRAMES + 6))

    assert microphone._queue.qsize() == RemoteMicrophone.QUEUE_FRAMES
    assert microphone._dropped == 6


def test_draining_clears_what_a_consumer_has_not_taken_yet() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.submit(silence(4))
    microphone.drain()

    assert microphone._queue.qsize() == 0


def test_the_level_tracks_the_most_recent_frame() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.submit(silence())
    assert microphone.level == pytest.approx(0.0)

    microphone.submit(tone(amplitude=16000))
    assert microphone.level > 0.5


async def test_silence_on_the_wire_yields_an_idle_tick_not_a_stall() -> None:
    """The listen loop treats b'' as 'nothing happened, carry on'. Without it a
    quiet room would park the loop inside the iterator forever, and a detach
    would have nothing to interrupt."""
    microphone = RemoteMicrophone()
    microphone.start()
    iterator = microphone.frames()

    assert await asyncio.wait_for(anext(iterator), timeout=2.0) == b""


async def test_a_submitted_frame_reaches_the_iterator() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    microphone.submit(tone())
    iterator = microphone.frames()

    assert len(await asyncio.wait_for(anext(iterator), timeout=2.0)) == FRAME_BYTES


async def test_stopping_ends_the_iterator_rather_than_leaving_it_parked() -> None:
    microphone = RemoteMicrophone()
    microphone.start()
    iterator = microphone.frames()
    consumed = asyncio.ensure_future(anext(iterator))
    await asyncio.sleep(0)

    microphone.stop()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(consumed, timeout=2.0)


def test_a_source_that_stops_sending_goes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A panel that loses power never sends a detach. Without staleness the
    core would sit holding a microphone that will never produce another frame —
    deaf, while reporting itself healthy."""
    microphone = RemoteMicrophone()
    microphone.start()
    assert microphone.stale is False

    clock = [1000.0]
    monkeypatch.setattr("nova.voice.remote.time.monotonic", lambda: clock[0])
    microphone.submit(silence())
    clock[0] += RemoteMicrophone.STALE_SECONDS + 1

    assert microphone.stale is True


# ------------------------------------------------------------------ speaker


def decode_wav(encoded: str) -> tuple[Any, int]:
    with wave.open(io.BytesIO(base64.b64decode(encoded)), "rb") as handle:
        rate = handle.getframerate()
        pcm = handle.readframes(handle.getnframes())
    return numpy.frombuffer(pcm, dtype=numpy.int16), rate


async def test_speaking_publishes_a_playable_clip_with_its_own_duration() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    speaker = RemoteSpeaker(lambda topic, payload: published.append((topic, payload)))
    samples = numpy.zeros(8000, dtype=numpy.float32)

    playing = asyncio.ensure_future(speaker.play(samples, 16000))
    await asyncio.sleep(0.05)

    assert published[0][0] == REMOTE_PLAY
    assert published[0][1]["sampleRate"] == 16000
    assert published[0][1]["durationMs"] == 500  # 8000 samples at 16 kHz
    decoded, rate = decode_wav(published[0][1]["wav"])
    assert rate == 16000 and len(decoded) == 8000

    speaker.cancel()
    await playing


async def test_volume_is_applied_before_the_clip_leaves_the_core() -> None:
    """So one `voice.tts.volume` setting means the same thing on a panel as it
    does through a local speaker, rather than each device inventing its own."""
    published: list[tuple[str, dict[str, Any]]] = []
    speaker = RemoteSpeaker(lambda t, p: published.append((t, p)), volume=0.5)
    samples = numpy.full(1600, 0.8, dtype=numpy.float32)

    playing = asyncio.ensure_future(speaker.play(samples, 16000))
    await asyncio.sleep(0.05)

    decoded, _ = decode_wav(published[0][1]["wav"])
    assert int(decoded[0]) == pytest.approx(int(0.4 * 32767), abs=2)

    speaker.cancel()
    await playing


async def test_a_barge_in_stops_playback_early_and_tells_the_device() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    speaker = RemoteSpeaker(lambda t, p: published.append((t, p)))
    # Ten seconds of audio: if cancelling did not work, this test would block
    # for the full clip rather than returning promptly.
    samples = numpy.zeros(160000, dtype=numpy.float32)

    playing = asyncio.ensure_future(speaker.play(samples, 16000))
    await asyncio.sleep(0.05)
    assert speaker.playing is True
    speaker.cancel()
    await asyncio.wait_for(playing, timeout=2.0)

    assert [topic for topic, _ in published] == [REMOTE_PLAY, REMOTE_STOP]
    assert speaker.playing is False


async def test_playback_ends_on_its_own_when_nothing_interrupts() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    speaker = RemoteSpeaker(lambda t, p: published.append((t, p)))

    await asyncio.wait_for(speaker.play(numpy.zeros(160, dtype=numpy.float32), 16000), timeout=3.0)

    # No stop event: the clip was allowed to finish, so the device needs no
    # instruction beyond the one it already has.
    assert [topic for topic, _ in published] == [REMOTE_PLAY]


# ------------------------------------------------------- service integration


async def test_attaching_gives_the_core_a_microphone_it_did_not_have(
    ctx: NovaContext,
) -> None:
    """The headless case, and the whole reason this exists: a box in a rack
    reports no microphone until a panel offers one."""
    voice = VoiceService(ctx)
    voice._degraded["microphone"] = "no sound hardware"
    assert voice.capabilities["microphone"] is False

    result = await voice.attach_remote()

    assert voice.remote_attached is True
    assert voice.capabilities["microphone"] is True
    assert result["frameBytes"] == FRAME_BYTES
    assert result["sampleRate"] == 16000
    assert result["sessionId"]
    await quiesce(voice)


async def test_attaching_is_refused_when_the_setting_is_off(ctx: NovaContext) -> None:
    ctx.store.patch({"voice": {"audio": {"allow_remote": False}}}, persist=False)
    voice = VoiceService(ctx)

    with pytest.raises(NovaError, match="remote audio is disabled"):
        await voice.attach_remote()


async def test_a_reattach_replaces_the_previous_session(ctx: NovaContext) -> None:
    """A panel that rebooted never detached. The newest attach has to win —
    the old session is gone whether or not it managed to say so."""
    voice = VoiceService(ctx)
    first = await voice.attach_remote()

    second = await voice.attach_remote()

    assert second["sessionId"] != first["sessionId"]
    assert voice.remote_attached is True
    await quiesce(voice)


async def test_frames_from_a_stale_session_are_refused(ctx: NovaContext) -> None:
    """Refusing is what tells a reconnected panel to attach again, rather than
    letting it push audio into a session the core has already replaced."""
    voice = VoiceService(ctx)
    await voice.attach_remote()
    await voice.attach_remote()

    with pytest.raises(NovaError, match="stale audio session"):
        voice.submit_remote_frame("a-session-that-has-been-replaced", silence())
    await quiesce(voice)


async def test_frames_are_refused_when_nothing_is_attached(ctx: NovaContext) -> None:
    voice = VoiceService(ctx)

    with pytest.raises(NovaError, match="no remote audio source"):
        voice.submit_remote_frame("", silence())


async def test_frames_are_accepted_for_the_current_session(ctx: NovaContext) -> None:
    voice = VoiceService(ctx)
    session = (await voice.attach_remote())["sessionId"]

    assert voice.submit_remote_frame(session, silence(2)) == 2
    await quiesce(voice)


async def test_detaching_hands_the_job_back_and_reports_the_lack_of_hardware(
    ctx: NovaContext,
) -> None:
    """There is no PortAudio in the test environment, which is exactly the
    headless case — detaching must degrade honestly rather than raise."""
    voice = VoiceService(ctx)
    await voice.attach_remote()

    assert await voice.detach_remote() is True
    assert voice.remote_attached is False
    assert voice.capabilities["microphone"] is False
    await quiesce(voice)


async def test_detaching_when_nothing_is_attached_is_a_no_op(ctx: NovaContext) -> None:
    voice = VoiceService(ctx)
    assert await voice.detach_remote() is False


async def test_the_wake_word_fires_on_audio_from_the_other_device(
    ctx: NovaContext,
) -> None:
    """The payoff. Frames that arrived over a socket run the same detector, and
    reach the same state machine, as frames off a local sound card — which is
    what makes a wall panel a full client rather than a push-to-talk remote."""
    ctx.store.patch({"assistant": {"wake_response": ""}}, persist=False)
    voice = VoiceService(ctx)
    voice.wake = AlwaysDetects()  # type: ignore[assignment]
    session = (await voice.attach_remote())["sessionId"]
    await ctx.state.transition(NovaState.IDLE)

    voice.submit_remote_frame(session, tone())
    await asyncio.sleep(0.1)  # let the listen loop take it off the queue

    assert ctx.state.state is NovaState.LISTENING
    assert voice._state is ListenState.CAPTURING
    await quiesce(voice)


async def test_speaking_tells_the_device_to_stop_capturing_and_then_resume(
    ctx: NovaContext,
) -> None:
    """Server-side muting is enough to be correct, but the device also has to
    be told: it is the only place our own voice can be kept out of the
    microphone rather than merely discarded after the fact."""
    voice = VoiceService(ctx)
    await voice.attach_remote()
    captured: list[bool] = []
    ctx.bus.subscribe(REMOTE_CAPTURE, lambda event: captured.append(event.payload["capture"]))

    voice._set_capture(False)
    voice._set_capture(True)

    assert captured == [False, True]
    await quiesce(voice)


async def test_a_stale_device_is_detached_by_the_watchdog(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = VoiceService(ctx)
    await voice.attach_remote()
    monkeypatch.setattr(type(voice._remote), "stale", property(lambda self: True))

    await asyncio.sleep(2.2)  # one watchdog tick

    assert voice.remote_attached is False
    await quiesce(voice)
