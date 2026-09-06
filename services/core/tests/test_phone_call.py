"""How a call behaves, tested without placing one.

Everything here runs against a fake transport. The alternative is a suite that
can only be run by ringing a real telephone, which means it never runs, which
means the interesting cases — being talked over, three wrong PINs, the far end
vanishing mid-sentence — are never checked at all.

Audio is fed in as μ-law packets exactly as Twilio delivers them, so the
resampling and framing are exercised on the way through rather than stubbed.
And it is fed *in reply to* N.O.V.A. speaking rather than on a timer: a
conversation is turn-taking, and a test that pushes audio on a fixed schedule
is racing the thing it is testing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
import pytest

from nova.config.schema import PhoneSettings, VoiceSettings
from nova.phone.audio import Downsampler, pcm16_to_mulaw
from nova.phone.call import Call, CallOutcome, Ending, is_farewell

PACKET_SAMPLES = 160  # 20 ms at 8 kHz, which is what a phone line delivers


def quick_settings(**overrides: object) -> PhoneSettings:
    """Phone settings with the timeouts wound right down.

    Built past validation on purpose: the real minimums exist so nobody
    configures a line that hangs up after a two-second pause, and a suite that
    waited out the real ones would take minutes.
    """
    values: dict[str, object] = {"silence_hangup_seconds": 0.8, "max_call_seconds": 30.0}
    values.update(overrides)
    return PhoneSettings.model_construct(**values)


class FakeLine:
    """A `CallTransport` that records instead of dialling."""

    def __init__(self) -> None:
        self.played: list[np.ndarray] = []
        self.cleared = 0
        self.hung_up = False
        self.live = True
        #: Delay playback, so a test has something to interrupt.
        self.play_seconds = 0.0
        #: Called shortly after each reply — the far end taking its turn.
        self.on_reply: Callable[[], Awaitable[None]] | None = None
        self._followups: set[asyncio.Task[None]] = set()

    async def play(self, samples: np.ndarray) -> None:
        self.played.append(samples)
        if self.play_seconds:
            await asyncio.sleep(self.play_seconds)
        if self.on_reply is not None:
            task = asyncio.create_task(self._take_a_turn())
            self._followups.add(task)
            task.add_done_callback(self._followups.discard)

    async def _take_a_turn(self) -> None:
        # Only after `_say` has stopped listening for an interruption, or the
        # answer would be heard as the far end talking over it.
        await asyncio.sleep(0.08)
        if self.on_reply is not None and self.live:
            await self.on_reply()

    async def stop_playing(self) -> None:
        self.cleared += 1

    async def hang_up(self) -> None:
        self.hung_up = True
        self.live = False
        for task in tuple(self._followups):
            task.cancel()


def speech_packet(loud: bool = True) -> bytes:
    """One 20 ms μ-law packet: a tone the voice detector accepts, or silence."""
    if not loud:
        return pcm16_to_mulaw(np.zeros(PACKET_SAMPLES, dtype=np.int16))
    t = np.arange(PACKET_SAMPLES) / 8000
    return pcm16_to_mulaw((np.sin(2 * np.pi * 300 * t) * 9000).astype(np.int16))


async def feed(call: Call, packets: int, *, loud: bool = True) -> None:
    for _ in range(packets):
        call.submit(speech_packet(loud))
        await asyncio.sleep(0)


async def utterance(call: Call, *, packets: int = 30) -> None:
    """Somebody says something: quiet, then speech, then quiet again.

    The leading quiet matters. Without webrtcvad installed the endpointer falls
    back to an adaptive noise gate that takes its floor from the first frame it
    sees — so an utterance opening at full volume sets the floor to its own
    level and is never heard as speech at all. A real line opens with room
    tone, which is what this imitates.
    """
    await feed(call, 10, loud=False)
    await feed(call, packets)
    await feed(call, 25, loud=False)


def build(
    line: FakeLine,
    *,
    heard: list[str],
    replies: list[str] | None = None,
    **kwargs: Any,
) -> tuple[Call, list[str]]:
    """A call whose transcriber returns `heard` in order, one per utterance."""
    said: list[str] = []
    pending = list(heard)
    answers = list(replies or [])

    async def transcribe(_audio: bytes) -> str:
        return pending.pop(0) if pending else ""

    async def synthesise(text: str) -> np.ndarray:
        said.append(text)
        return np.zeros(1600, dtype=np.int16)

    async def respond(_text: str) -> str:
        return answers.pop(0) if answers else "Right."

    call = Call(
        transport=line,
        transcribe=transcribe,
        synthesise=synthesise,
        respond=respond,
        settings=quick_settings(),
        voice=VoiceSettings(),
        **kwargs,
    )
    return call, said


async def converse(line: FakeLine, call: Call, *, opens: bool) -> CallOutcome:
    """Run the call with the far end replying each time it is spoken to.

    `opens` says whether N.O.V.A. speaks first. When it does, its opening line
    prompts the first response; when it does not, somebody has to say something
    before there is anything to reply to.
    """
    line.on_reply = lambda: utterance(call)
    kick = None if opens else asyncio.create_task(_first_word(call))
    try:
        return await asyncio.wait_for(call.run(), timeout=20)
    finally:
        if kick is not None:
            kick.cancel()


async def _first_word(call: Call) -> None:
    await asyncio.sleep(0.05)
    await utterance(call)


# ------------------------------------------------------------------ farewells


@pytest.mark.parametrize(
    "text", ["bye", "Goodbye.", "that's all", "Thanks, bye!", "nothing else", "hang up"]
)
def test_a_goodbye_ends_the_call(text: str) -> None:
    assert is_farewell(text)


@pytest.mark.parametrize(
    "text",
    [
        "say goodbye to that idea",
        "what did I spend at Bye Bye Baby",
        "buy some milk",
        "that's all I spent on it, how much is left",
    ],
)
def test_a_goodbye_inside_a_sentence_does_not(text: str) -> None:
    """Matched whole rather than searched for. "That's all I spent on it" is a
    question, and hanging up on it would be maddening."""
    assert not is_farewell(text)


# ---------------------------------------------------------------- the exchange


async def test_an_outbound_call_says_why_it_rang_before_anything_else() -> None:
    """The brief's requirement, and the reason `opening` exists: a call that
    starts with "hello?" and waits is a nuisance call from your own house."""
    line = FakeLine()
    call, said = build(
        line,
        heard=["bye"],
        opening="Good evening sir, I noticed a spend of 50 pounds at Currys.",
    )

    outcome = await converse(line, call, opens=True)

    assert said[0].startswith("Good evening sir")
    assert outcome.ending is Ending.GOODBYE


async def test_the_conversation_continues_until_a_goodbye() -> None:
    """No wake word and no six-second window: it keeps going."""
    line = FakeLine()
    call, said = build(
        line,
        heard=["how much have I got", "and what's committed", "bye"],
        replies=["340 pounds available.", "60 pounds for the phone bill."],
    )

    outcome = await converse(line, call, opens=False)

    assert outcome.turns == 2, "two questions answered, then the goodbye"
    assert "340 pounds available." in said
    assert "60 pounds for the phone bill." in said
    assert outcome.ending is Ending.GOODBYE


async def test_silence_ends_the_call_rather_than_hanging_there() -> None:
    line = FakeLine()
    call, _ = build(line, heard=[])

    outcome = await asyncio.wait_for(call.run(), timeout=20)

    assert outcome.ending is Ending.SILENCE
    assert line.hung_up


async def test_the_far_end_hanging_up_ends_it() -> None:
    line = FakeLine()
    call, _ = build(line, heard=[])

    async def hang_up() -> None:
        await asyncio.sleep(0.05)
        line.live = False

    task = asyncio.create_task(hang_up())
    outcome = await asyncio.wait_for(call.run(), timeout=20)
    await task

    assert outcome.ending in (Ending.HUNG_UP, Ending.SILENCE)


async def test_a_call_cannot_run_forever() -> None:
    """A stuck call is billed by the minute and unnerving to be on."""
    line = FakeLine()
    call, said = build(line, heard=["still here"])
    call._settings = quick_settings(max_call_seconds=0)

    outcome = await asyncio.wait_for(call.run(), timeout=20)

    assert outcome.ending is Ending.TOO_LONG
    assert any("let you go" in phrase for phrase in said)


# ------------------------------------------------------------------- barge-in


async def test_talking_over_it_stops_it_talking() -> None:
    """The thing that makes a voice system feel like a machine is carrying on
    regardless. Speech during playback drops whatever the far end has not heard
    yet."""
    line = FakeLine()
    line.play_seconds = 2.0  # a long reply, so there is something to interrupt
    call, _ = build(line, heard=["how much have I got", "bye"])

    async def interrupt() -> None:
        await asyncio.sleep(0.05)
        await utterance(call)  # the question
        await asyncio.sleep(0.4)  # it starts answering
        await feed(call, 10, loud=False)
        await feed(call, 40)  # talked over

    task = asyncio.create_task(interrupt())
    await asyncio.wait_for(call.run(), timeout=20)
    task.cancel()

    assert line.cleared >= 1, "the unheard part of the reply was not discarded"


async def test_a_single_noise_does_not_count_as_an_interruption() -> None:
    """One frame is a cough, a door, or the line itself. Three is somebody with
    something to say."""
    line = FakeLine()
    line.play_seconds = 0.6
    call, _ = build(line, heard=["hello", "bye"])

    async def blip() -> None:
        await asyncio.sleep(0.05)
        await utterance(call)
        await asyncio.sleep(0.25)
        await feed(call, 2)  # well under one 80 ms frame of speech

    task = asyncio.create_task(blip())
    await asyncio.wait_for(call.run(), timeout=20)
    task.cancel()

    assert line.cleared == 0


# ----------------------------------------------------------------------- PIN


async def test_an_inbound_call_is_challenged_before_anything_financial() -> None:
    """Anyone who learns the number can dial it, and caller ID is a field the
    caller fills in, not evidence."""
    line = FakeLine()
    call, said = build(
        line,
        heard=["one two three four", "how much have I got", "bye"],
        challenge="What's your PIN?",
        accepts=lambda text: "one two three four" in text,
    )

    outcome = await converse(line, call, opens=True)

    assert said[0] == "What's your PIN?"
    assert outcome.ending is Ending.GOODBYE
    assert outcome.turns == 1


async def test_a_wrong_pin_gets_three_tries_and_then_the_door() -> None:
    line = FakeLine()
    call, said = build(
        line,
        heard=["nine nine nine nine"] * 3,
        challenge="What's your PIN?",
        accepts=lambda text: "one two three four" in text,
    )

    outcome = await converse(line, call, opens=True)

    assert outcome.ending is Ending.REFUSED
    assert outcome.turns == 0, "nothing was answered"
    assert said.count("Sorry, try again.") == 2
    assert "I can't help without that. Goodbye." in said


async def test_no_pin_configured_means_no_challenge() -> None:
    """Outbound calls dial a number you configured, so there is nobody to
    authenticate — the challenge is skipped rather than asked and ignored."""
    line = FakeLine()
    call, said = build(line, heard=["bye"], opening="Good evening.")

    await converse(line, call, opens=True)

    assert not any("PIN" in phrase for phrase in said)


# --------------------------------------------------------------------- audio


async def test_line_audio_arrives_as_frames_the_transcriber_can_use() -> None:
    """μ-law packets in, 16 kHz frames out, with the resampling done on the way
    through rather than at the edges."""
    line = FakeLine()
    captured: list[bytes] = []

    async def transcribe(audio: bytes) -> str:
        captured.append(audio)
        return "bye"

    async def synthesise(_text: str) -> np.ndarray:
        return np.zeros(160, dtype=np.int16)

    async def respond(_text: str) -> str:
        return "ok"

    call = Call(
        transport=line,
        transcribe=transcribe,
        synthesise=synthesise,
        respond=respond,
        settings=quick_settings(),
        voice=VoiceSettings(),
    )

    await converse(line, call, opens=False)

    assert captured, "nothing reached the transcriber"
    samples = np.frombuffer(captured[0], dtype=np.int16)
    assert samples.size > 8_000, "less than half a second of audio arrived"
    assert np.abs(samples).max() > 1000, "the audio arrived silent"


def test_what_goes_out_survives_the_trip_to_the_line() -> None:
    """The reply is synthesised at 16 kHz and has to arrive as something a
    telephone can carry."""
    t = np.arange(16000) / 16000
    speech = (np.sin(2 * np.pi * 440 * t) * 10000).astype(np.int16)

    packet = pcm16_to_mulaw(Downsampler().process(speech))

    assert len(packet) == 8000, "one second of 8 kHz μ-law is 8000 bytes"
