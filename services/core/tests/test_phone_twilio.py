"""The Twilio side: what gets said to it, and what gets believed from it.

The media stream is tested against a fake WebSocket carrying the exact JSON
envelopes Twilio sends, because the two envelope types that matter — `mark` and
`clear` — are the two easiest to get subtly wrong and the hardest to notice.
A missing mark does not fail, it makes N.O.V.A. start listening while it is
still talking.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import numpy as np
import pytest

from nova.phone.audio import mulaw_to_pcm16
from nova.phone.stream import PACKET_BYTES, MediaStream
from nova.phone.twilio import signature_matches, twiml_connect, twiml_reject

TOKEN = "an-auth-token"


# ------------------------------------------------------------------- signature


def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(key + params[key] for key in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


def test_a_genuine_request_is_accepted() -> None:
    url = "https://x.trycloudflare.com/phone/answer"
    params = {"CallSid": "CA1", "From": "+447700900123"}

    assert signature_matches(TOKEN, url, params, twilio_signature(TOKEN, url, params))


def test_a_tampered_parameter_is_refused() -> None:
    """The signature covers the parameters, so somebody cannot keep a captured
    signature and change who the call claims to be from."""
    url = "https://x.trycloudflare.com/phone/answer"
    signature = twilio_signature(TOKEN, url, {"From": "+447700900123"})

    assert not signature_matches(TOKEN, url, {"From": "+447700900999"}, signature)


def test_a_different_url_is_refused() -> None:
    params = {"CallSid": "CA1"}
    signature = twilio_signature(TOKEN, "https://x.trycloudflare.com/phone/answer", params)

    assert not signature_matches(TOKEN, "https://evil.example/phone/answer", params, signature)


@pytest.mark.parametrize("token,provided", [("", "abc"), (TOKEN, ""), ("", "")])
def test_nothing_missing_is_ever_treated_as_a_match(token: str, provided: str) -> None:
    """An unconfigured token must not make every request valid — that turns a
    misconfiguration into an endpoint anyone can drive."""
    assert not signature_matches(token, "https://x/answer", {}, provided)


# ----------------------------------------------------------------------- TwiML


def test_the_answer_opens_a_two_way_stream() -> None:
    """`Connect` and not `Start`. The latter forks a copy of the caller's audio
    to a socket and is one-way — N.O.V.A. would listen and never be able to
    reply, which looks exactly like a broken microphone."""
    xml = twiml_connect("wss://x.trycloudflare.com/phone/stream")

    assert "<Connect>" in xml
    assert "<Start>" not in xml
    assert 'url="wss://x.trycloudflare.com/phone/stream"' in xml


def test_a_url_with_an_ampersand_does_not_break_the_xml() -> None:
    xml = twiml_connect("wss://x/stream?a=1&b=2")

    assert "&amp;" in xml
    assert "&b=2" not in xml


def test_a_refused_call_says_so_and_hangs_up() -> None:
    xml = twiml_reject("Sorry, I can only speak to the account holder.")

    assert "<Hangup />" in xml
    assert "account holder" in xml


# ----------------------------------------------------------------- the stream


class FakeSocket:
    """A Twilio media stream socket, scripted."""

    def __init__(self, incoming: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._incoming = list(incoming or [])
        #: Set when the test wants to answer a mark as Twilio would.
        self.auto_mark = False

    async def send(self, raw: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        message = json.loads(raw)
        self.sent.append(message)
        if self.auto_mark and message.get("event") == "mark":
            self._incoming.append({"event": "mark", "mark": message["mark"]})

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        while not self._incoming:
            await asyncio.sleep(0.01)
            if self.closed:
                raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))


def start_event(sid: str = "MZ1") -> dict:
    return {
        "event": "start",
        "streamSid": sid,
        "start": {"streamSid": sid, "callSid": "CA1", "tracks": ["inbound"]},
    }


def media_event(payload: bytes) -> dict:
    return {
        "event": "media",
        "media": {"track": "inbound", "payload": base64.b64encode(payload).decode()},
    }


async def test_incoming_audio_is_handed_on_as_mu_law() -> None:
    heard: list[bytes] = []
    packet = bytes(range(PACKET_BYTES))
    socket = FakeSocket([start_event(), media_event(packet), {"event": "stop"}])
    stream = MediaStream(socket)

    await asyncio.wait_for(stream.read(heard.append), timeout=5)

    assert heard == [packet]
    assert stream.call_sid == "CA1"
    assert not stream.live, "a stop event ends the call"


async def test_a_malformed_envelope_does_not_end_the_call() -> None:
    """Twilio adds fields over time and the odd frame arrives mangled. Neither
    is a reason to drop a call in progress."""
    heard: list[bytes] = []
    socket = FakeSocket([start_event(), {"event": "surprise"}, media_event(b"\xff" * 8)])
    socket._incoming.append({"event": "stop"})
    stream = MediaStream(socket)

    await asyncio.wait_for(stream.read(heard.append), timeout=5)

    assert heard == [b"\xff" * 8]


async def test_outgoing_audio_is_packetised_the_way_twilio_expects() -> None:
    socket = FakeSocket([start_event()])
    socket.auto_mark = True
    stream = MediaStream(socket)
    reader = asyncio.create_task(stream.read(lambda _p: None))
    await asyncio.sleep(0.05)

    # Half a second of 16 kHz speech becomes 4000 bytes of 8 kHz μ-law.
    t = np.arange(8000) / 16000
    await asyncio.wait_for(
        stream.play((np.sin(2 * np.pi * 440 * t) * 9000).astype(np.int16)), timeout=5
    )

    media = [m for m in socket.sent if m["event"] == "media"]
    assert len(media) == 25, "4000 bytes in 160-byte packets"
    for message in media:
        assert message["streamSid"] == "MZ1"
        assert len(base64.b64decode(message["media"]["payload"])) == PACKET_BYTES
    # And it is audible rather than silence.
    first = mulaw_to_pcm16(base64.b64decode(media[0]["media"]["payload"]))
    assert np.abs(first).max() > 1000

    await stream.hang_up()
    reader.cancel()


async def test_playback_waits_for_the_mark_to_come_back() -> None:
    """Audio sent is not audio heard — Twilio buffers it. Returning as soon as
    the bytes are written means N.O.V.A. starts listening while it is still
    talking, and hears itself."""
    socket = FakeSocket([start_event()])
    stream = MediaStream(socket)
    reader = asyncio.create_task(stream.read(lambda _p: None))
    await asyncio.sleep(0.05)

    playing = asyncio.create_task(stream.play(np.zeros(1600, dtype=np.int16)))
    await asyncio.sleep(0.1)

    assert not playing.done(), "it should still be waiting for the mark"
    mark = next(m for m in socket.sent if m["event"] == "mark")
    socket._incoming.append({"event": "mark", "mark": mark["mark"]})
    await asyncio.wait_for(playing, timeout=5)

    await stream.hang_up()
    reader.cancel()


async def test_an_interruption_clears_the_buffer_and_frees_the_wait() -> None:
    """`clear` discards what Twilio has buffered — which also discards the mark
    queued behind it. Waiting for a mark that can never arrive would hang the
    call until the timeout, which is the whole length of the reply the person
    just interrupted."""
    socket = FakeSocket([start_event()])
    stream = MediaStream(socket)
    reader = asyncio.create_task(stream.read(lambda _p: None))
    await asyncio.sleep(0.05)

    playing = asyncio.create_task(stream.play(np.zeros(16000, dtype=np.int16)))
    await asyncio.sleep(0.1)
    await stream.stop_playing()

    await asyncio.wait_for(playing, timeout=5)
    assert any(m["event"] == "clear" for m in socket.sent)

    await stream.hang_up()
    reader.cancel()


async def test_keypad_digits_are_collected() -> None:
    """A PIN is far easier to key than to say down a phone line, and this way
    Whisper does not have to be right about "one two three four"."""
    socket = FakeSocket(
        [
            start_event(),
            {"event": "dtmf", "dtmf": {"digit": "1"}},
            {"event": "dtmf", "dtmf": {"digit": "2"}},
            {"event": "stop"},
        ]
    )
    stream = MediaStream(socket)

    await asyncio.wait_for(stream.read(lambda _p: None), timeout=5)

    assert stream.digits == ["1", "2"]


async def test_a_dead_socket_stops_playback_rather_than_raising() -> None:
    """The far end hanging up mid-reply is ordinary, not an error."""
    socket = FakeSocket([start_event()])
    stream = MediaStream(socket)
    reader = asyncio.create_task(stream.read(lambda _p: None))
    await asyncio.sleep(0.05)
    socket.closed = True

    await asyncio.wait_for(stream.play(np.zeros(1600, dtype=np.int16)), timeout=5)

    assert not stream.live
    reader.cancel()
