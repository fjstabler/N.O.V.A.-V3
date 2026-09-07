"""Twilio Media Streams: the socket a live call arrives on.

Twilio opens a WebSocket to this machine and sends the caller's audio as
base64 μ-law in 20 ms packets, wrapped in small JSON envelopes. Sending audio
back is the same thing in reverse. Two of its five message types do the real
work here and both are easy to miss:

**`mark`.** Audio sent is not audio heard — Twilio buffers it and plays it out
at real time. A mark placed after a reply comes back when the far end has
actually finished hearing it, which is the only way to know when to start
listening again. Guessing from the sample count works until the network is
slow, at which point N.O.V.A. starts listening to itself.

**`clear`.** Discards whatever is still buffered. It is what makes an
interruption an interruption: without it, being talked over stops nothing, and
the reply keeps playing over the person for however long it had left.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any

import numpy as np

from ..runtime.logging import get_logger
from .audio import Downsampler, pcm16_to_mulaw

log = get_logger(__name__)

#: 20 ms of 8 kHz μ-law, which is the packet size Twilio speaks in.
PACKET_BYTES = 160

#: Long enough for a slow network, short enough that a lost mark does not hang
#: the conversation. A mark that never arrives means playback finished as far
#: as this is concerned.
MARK_TIMEOUT = 30.0


class MediaStream:
    """One Twilio media stream, presented as a `CallTransport`."""

    def __init__(self, websocket: Any) -> None:
        self._ws = websocket
        self._downsampler = Downsampler()
        self._stream_sid = ""
        self._call_sid = ""
        self._live = True
        self._marks: dict[str, asyncio.Event] = {}
        self._mark_count = 0
        #: Digits pressed during the call, oldest first. A PIN is far easier to
        #: key than to say down a phone line, and Whisper does not have to be
        #: right about "one two three four" for it to work.
        self.digits: list[str] = []

    # ------------------------------------------------------------- properties

    @property
    def live(self) -> bool:
        return self._live

    @property
    def call_sid(self) -> str:
        return self._call_sid

    # ---------------------------------------------------------------- reading

    async def read(self, on_audio: Any) -> None:
        """Pump the socket until the call ends, handing μ-law packets on.

        `on_audio` is called with raw μ-law bytes and must not block — it is
        `Call.submit`, which does its own buffering for exactly this reason.
        """
        try:
            async for raw in self._ws:
                if not self._handle(raw, on_audio):
                    break
        except Exception as exc:  # noqa: BLE001 - a dropped call is not an error
            log.info("media_stream_closed", reason=str(exc)[:120])
        finally:
            self._live = False
            # Anything waiting on a mark will never get it now.
            for event in self._marks.values():
                event.set()

    def _handle(self, raw: str | bytes, on_audio: Any) -> bool:
        """Process one envelope. False means the stream is over."""
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return True
        if not isinstance(message, dict):
            return True

        event = message.get("event")
        if event == "media":
            payload = (message.get("media") or {}).get("payload", "")
            with contextlib.suppress(ValueError, TypeError):
                on_audio(base64.b64decode(payload))
            return True
        if event == "start":
            start = message.get("start") or {}
            self._stream_sid = str(message.get("streamSid") or start.get("streamSid") or "")
            self._call_sid = str(start.get("callSid") or "")
            log.info("media_stream_started")
            return True
        if event == "mark":
            name = str((message.get("mark") or {}).get("name", ""))
            waiting = self._marks.get(name)
            if waiting is not None:
                waiting.set()
            return True
        if event == "dtmf":
            digit = str((message.get("dtmf") or {}).get("digit", ""))
            if digit:
                self.digits.append(digit)
            return True
        if event == "stop":
            log.info("media_stream_stopped")
            return False
        return True

    # ---------------------------------------------------------------- writing

    async def play(self, samples: np.ndarray) -> None:
        """Send 16 kHz PCM, returning once the far end has heard it."""
        if not self._live or samples.size == 0:
            return

        payload = pcm16_to_mulaw(self._downsampler.process(samples))
        for offset in range(0, len(payload), PACKET_BYTES):
            packet = payload[offset : offset + PACKET_BYTES]
            if not await self._send(
                {
                    "event": "media",
                    "streamSid": self._stream_sid,
                    "media": {"payload": base64.b64encode(packet).decode("ascii")},
                }
            ):
                return

        await self._await_playback()

    async def _await_playback(self) -> None:
        """Place a mark and wait for it to come back."""
        self._mark_count += 1
        name = f"nova-{self._mark_count}"
        arrived = asyncio.Event()
        self._marks[name] = arrived

        sent = await self._send(
            {"event": "mark", "streamSid": self._stream_sid, "mark": {"name": name}}
        )
        try:
            if sent:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(MARK_TIMEOUT):
                        await arrived.wait()
        finally:
            self._marks.pop(name, None)

    async def stop_playing(self) -> None:
        """Drop audio Twilio has buffered but not yet played.

        Also releases anything waiting on a mark: the marks that were queued
        behind the cleared audio are cleared with it and will never arrive, so
        waiting for one would hang the call until the timeout.
        """
        await self._send({"event": "clear", "streamSid": self._stream_sid})
        for event in self._marks.values():
            event.set()

    async def hang_up(self) -> None:
        self._live = False
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def _send(self, message: dict[str, Any]) -> bool:
        if not self._live:
            return False
        try:
            await self._ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001 - the far end hanging up is normal
            log.info("media_send_failed", reason=str(exc)[:120])
            self._live = False
            return False
        return True
