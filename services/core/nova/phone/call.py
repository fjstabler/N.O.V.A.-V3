"""One telephone conversation, from "hello" to the line going dead.

A call is the same assistant the panel talks to, driven differently. The three
differences are all in here:

**No wake word.** Answering the phone is the attention signal. The microphone
is open from connect to hang-up, and every utterance is addressed to N.O.V.A.
by definition.

**It runs to its natural end.** The panel listens for six seconds after
speaking and gives up; a call keeps going until somebody says goodbye, until
nobody has said anything for a while, or until the hard cap — which exists
because a stuck call is billed by the minute and unnerving to be on.

**Either side can interrupt.** Speech detected while N.O.V.A. is talking stops
it mid-sentence, discards what the far end had not heard yet, and treats the
interruption as the next thing said. Without that, talking over it does nothing
for however long the reply had left to run, which is the single thing that
makes a voice system feel like a machine.

The transport is abstract on purpose. Everything below is exercised against a
fake one, because the alternative is a test suite that can only run by placing
a telephone call.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

from ..runtime.logging import get_logger
from ..voice.audio import FRAME_BYTES
from ..voice.vad import FRAME_MS, Endpointer, EndpointState
from .audio import Upsampler, mulaw_to_pcm16

log = get_logger(__name__)

#: Consecutive speech frames before N.O.V.A. accepts that it is being talked
#: over. One frame is a cough, a door, or the line itself; three is somebody
#: with something to say.
BARGE_IN_FRAMES = 3

#: Said at the end of a call, whoever ended it.
FAREWELL = "Goodbye."


class Ending(StrEnum):
    """Why a call stopped. Recorded because "it just ended" is not diagnosable."""

    GOODBYE = "goodbye"
    SILENCE = "silence"
    HUNG_UP = "hung-up"
    TOO_LONG = "too-long"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(slots=True)
class CallOutcome:
    ending: Ending
    turns: int = 0
    seconds: float = 0.0
    transcript: list[tuple[str, str]] = field(default_factory=list)


class CallTransport(Protocol):
    """What a call needs from whatever is carrying it.

    Narrow by design: three verbs and a hang-up. A Twilio media stream
    implements it, and so does the fake the tests use.
    """

    async def play(self, samples: np.ndarray) -> None:
        """Send 16 kHz PCM and return once the far end has finished hearing it."""

    async def stop_playing(self) -> None:
        """Discard audio already sent but not yet heard."""

    async def hang_up(self) -> None: ...

    @property
    def live(self) -> bool:
        """False once the far end is gone."""
        ...


#: What ends a call when the person says it. Matched against the whole
#: utterance rather than searched for inside it, so "goodbye for now" ends the
#: call and "say goodbye to that idea" does not.
FAREWELLS = frozenset(
    {
        "bye",
        "goodbye",
        "good bye",
        "bye bye",
        "goodbye for now",
        "that's all",
        "thats all",
        "that's all thanks",
        "that's it",
        "thats it",
        "nothing else",
        "no that's all",
        "hang up",
        "thanks bye",
        "thank you bye",
        "thanks goodbye",
        "cheers bye",
    }
)


def is_farewell(text: str) -> bool:
    stripped = text.strip().lower().rstrip(".!?").replace(",", "")
    return stripped in FAREWELLS


class Call:
    """Drives one conversation over a `CallTransport`."""

    def __init__(
        self,
        *,
        transport: CallTransport,
        transcribe: Callable[[bytes], Awaitable[str]],
        synthesise: Callable[[str], Awaitable[np.ndarray | None]],
        respond: Callable[[str], Awaitable[str]],
        settings: Any,
        voice: Any,
        opening: str = "",
        challenge: str = "",
        accepts: Callable[[str], bool] | None = None,
    ) -> None:
        self._transport = transport
        self._transcribe = transcribe
        self._synthesise = synthesise
        self._respond = respond
        self._settings = settings
        self._voice = voice
        #: Spoken on connect, before listening. An outbound call says why it
        #: rang before it asks anything — see `phrasing.opening`.
        self._opening = opening
        #: Asked before anything else on an inbound call, when a PIN is set.
        self._challenge = challenge
        self._accepts = accepts

        self._frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._upsampler = Upsampler()
        self._pending = bytearray()
        self._transcript: list[tuple[str, str]] = []
        self._started = time.monotonic()

    # ------------------------------------------------------------------ intake

    def submit(self, payload: bytes) -> None:
        """Take one μ-law packet off the line.

        Called from the transport's read loop, so it must not block and must
        not raise: a full queue means the conversation is behind, and dropping
        the oldest audio is better than stalling the socket that carries it.
        """
        samples = self._upsampler.process(mulaw_to_pcm16(payload))
        self._pending.extend(samples.tobytes())

        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            if self._frames.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._frames.get_nowait()
            self._frames.put_nowait(frame)

    # ---------------------------------------------------------------- the call

    async def run(self) -> CallOutcome:
        try:
            return await self._converse()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call_failed")
            return self._outcome(Ending.FAILED)
        finally:
            with contextlib.suppress(Exception):
                await self._transport.hang_up()

    async def _converse(self) -> CallOutcome:
        if self._opening:
            await self._say(self._opening)

        if self._challenge and not await self._authenticate():
            return self._outcome(Ending.REFUSED)

        turns = 0
        while self._transport.live:
            if self._over_time():
                await self._say("I'll have to let you go, sorry. Goodbye.")
                return self._outcome(Ending.TOO_LONG, turns)

            heard = await self._listen()
            if heard is None:
                # Nobody said anything for the whole window. On a phone that is
                # a call that has finished without either party saying so.
                await self._say(FAREWELL)
                return self._outcome(Ending.SILENCE, turns)

            self._transcript.append(("caller", heard))
            if is_farewell(heard):
                await self._say(FAREWELL)
                return self._outcome(Ending.GOODBYE, turns)

            turns += 1
            reply = await self._respond(heard)
            if reply:
                self._transcript.append(("nova", reply))
                await self._say(reply)

        return self._outcome(Ending.HUNG_UP, turns)

    async def _authenticate(self) -> bool:
        """Ask for the PIN before anything financial is said.

        Inbound only. Anyone who learns the number can dial it, and caller ID
        is not evidence of anything — it is a field the caller fills in. Three
        attempts, because a phone line mangles digits often enough that one
        would be unusable.
        """
        for attempt in range(3):
            await self._say(self._challenge if attempt == 0 else "Sorry, try again.")
            heard = await self._listen()
            if heard is None:
                break
            if self._accepts is not None and self._accepts(heard):
                log.info("call_authenticated")
                # Say so. Going straight to silent listening leaves the caller
                # with no way to tell a correct PIN from a dead line, and the
                # natural response to that is to say "hello?" — which becomes
                # the first thing the assistant is asked to act on.
                await self._say("Thank you. What can I do for you?")
                return True
            log.warning("call_pin_rejected", attempt=attempt + 1)

        await self._say("I can't help without that. Goodbye.")
        return False

    # ------------------------------------------------------------- one exchange

    async def _listen(self) -> str | None:
        """Collect one utterance and transcribe it. None means nobody spoke."""
        stt = self._voice.stt
        endpointer = Endpointer(
            silence_ms=stt.silence_ms,
            max_utterance_seconds=stt.max_utterance_seconds,
            aggressiveness=stt.vad_aggressiveness,
            start_timeout_seconds=self._settings.silence_hangup_seconds,
        )
        buffer = bytearray()

        while self._transport.live:
            frame = await self._next_frame()
            if frame is None:
                return None
            buffer.extend(frame)
            result = endpointer.feed(frame)
            if result.state is EndpointState.DONE:
                break
            if result.state is EndpointState.TIMEOUT:
                return None

        if not endpointer.had_speech or len(buffer) < FRAME_BYTES:
            return None
        text = (await self._transcribe(bytes(buffer))).strip()
        log.info("call_heard", characters=len(text))
        return text or None

    async def _say(self, text: str) -> None:
        """Speak, and stop the moment the far end starts talking over it."""
        samples = await self._synthesise(text)
        if samples is None or not self._transport.live:
            return

        playing = asyncio.create_task(self._transport.play(samples))
        watching = asyncio.create_task(self._watch_for_interruption())

        done, _ = await asyncio.wait({playing, watching}, return_when=asyncio.FIRST_COMPLETED)

        if watching in done and not watching.cancelled() and watching.result():
            # Interrupted. Drop what the far end has not heard yet, or it goes
            # on talking over the person for the length of the unplayed reply.
            playing.cancel()
            with contextlib.suppress(Exception):
                await self._transport.stop_playing()
            log.info("call_interrupted")
        else:
            watching.cancel()

        for task in (playing, watching):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _watch_for_interruption(self) -> bool:
        """True once the far end has been speaking for long enough to mean it.

        Borrows the endpointer for its voice detector only — the thresholds are
        set so its state machine never fires. What is wanted is the per-frame
        verdict, and `silence_ms` back at zero is exactly that: `feed` resets it
        on any frame it considers speech.

        `had_speech` cannot be used for this. It latches on the first speech
        frame and stays true, so counting on it would report an interruption
        for every frame after the first cough.
        """
        detector = Endpointer(
            silence_ms=10_000,
            max_utterance_seconds=3600,
            aggressiveness=self._voice.stt.vad_aggressiveness,
            start_timeout_seconds=3600,
        )
        consecutive = 0
        while True:
            frame = await self._next_frame()
            if frame is None:
                return False
            spoke = detector.feed(frame).silence_ms == 0
            consecutive = consecutive + 1 if spoke else 0
            if consecutive >= BARGE_IN_FRAMES:
                return True

    async def _next_frame(self) -> bytes | None:
        """One 80 ms frame, or None once the line is dead.

        A live line that has stopped delivering gets silence synthesised at the
        rate real frames would have arrived. The endpointer measures time in
        frames fed, not by the clock, so a slower substitution would stretch
        every silence threshold — twelve seconds of quiet would take a minute
        to notice.
        """
        # `asyncio.timeout` rather than `wait_for`, and the difference is not
        # stylistic. `wait_for` reports an external cancellation as TimeoutError
        # when the two race — so `except TimeoutError` here swallowed the
        # cancellation, the loop carried on, and the task could never be
        # stopped again because its cancel request had already been consumed.
        # It showed up as a call that would not hang up, only once the queue
        # was empty enough for the timeout to fire constantly.
        try:
            async with asyncio.timeout(FRAME_MS / 1000):
                return await self._frames.get()
        except TimeoutError:
            return None if not self._transport.live else b"\x00" * FRAME_BYTES

    # ---------------------------------------------------------------- bookkeeping

    def _over_time(self) -> bool:
        return (time.monotonic() - self._started) > self._settings.max_call_seconds

    def _outcome(self, ending: Ending, turns: int = 0) -> CallOutcome:
        seconds = round(time.monotonic() - self._started, 1)
        log.info("call_ended", ending=ending.value, turns=turns, seconds=seconds)
        return CallOutcome(
            ending=ending, turns=turns, seconds=seconds, transcript=list(self._transcript)
        )


__all__ = [
    "BARGE_IN_FRAMES",
    "Call",
    "CallOutcome",
    "CallTransport",
    "Ending",
    "is_farewell",
]
