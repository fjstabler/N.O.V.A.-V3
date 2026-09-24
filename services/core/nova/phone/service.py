"""The telephone, wired into the rest of N.O.V.A.

Owns a listener of its own on a port of its own. That is deliberate: this is
the one socket in the system that is deliberately reachable from the internet,
and putting it on the bridge's port would expose the settings panel, the camera
snapshots and the whole assistant protocol along with it. Here, the only things
behind the public address are a TwiML document and a media stream.

Two ways a call happens:

*Inbound* — somebody dials the number. Twilio fetches `/phone/answer`, which
returns TwiML pointing back at `/phone/stream`, and the conversation begins
with a PIN challenge.

*Outbound* — a large debit lands, or something else decides it is worth
ringing. The call is placed through the REST API with an answer URL carrying a
token; when Twilio calls that URL back the token says which pending intent it
belongs to, and therefore what the opening line should be.

The public address comes from a tunnel started here, because a home connection
has no address of its own and a free tunnel hands out a different hostname
every time it comes up. Rather than ask somebody to keep a console in sync with
that, the URL is re-registered with Twilio at every start.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np

from ..context import NovaContext
from ..finance import secrets as finance_secrets
from ..runtime import Service
from ..runtime.errors import DegradedCapability, SkillError
from ..voice.audio import SAMPLE_RATE
from . import phrasing
from .call import Call, CallOutcome, Ending
from .stream import MediaStream
from .twilio import TwilioClient, signature_matches, twiml_connect, twiml_reject

try:  # pragma: no cover - exercised by the bridge's own import guard
    from websockets.asyncio.server import ServerConnection, serve
    from websockets.datastructures import Headers
    from websockets.http11 import Request, Response
except ImportError:  # pragma: no cover
    serve = None  # type: ignore[misc,assignment]

#: How long a pending outbound intent stays claimable. Twilio calls the answer
#: URL within seconds of the call connecting; anything older is a stale token.
INTENT_TTL = 300.0

#: Where cloudflared prints the hostname it was given.
_QUICK_TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


@dataclass(slots=True)
class Intent:
    """Why an outbound call was placed, waiting for it to be answered."""

    opening: str
    created_at: float
    #: Handles the reply to `opening`, before the model sees anything. Returns
    #: what to say back, or None to let the model take it from there.
    first_reply: Any = None
    context: dict[str, Any] = field(default_factory=dict)


class PhoneService(Service):
    """Answers and places calls."""

    name = "phone"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self._server: Any = None
        self._tunnel: asyncio.subprocess.Process | None = None
        self._public_url = ""
        self._client: TwilioClient | None = None
        self._intents: dict[str, Intent] = {}
        self._token = ""
        self._active: set[asyncio.Task[None]] = set()

    # -------------------------------------------------------------- lifecycle

    async def on_start(self) -> None:
        settings = self.ctx.settings.phone
        if not settings.enabled:
            raise DegradedCapability("phone", "disabled in settings")
        if serve is None:  # pragma: no cover
            raise DegradedCapability("phone", "the 'websockets' package is required")

        store = finance_secrets.load(self.ctx.paths.data_dir)
        self._token = store.get("NOVA_TWILIO_TOKEN")
        if not self._token or not settings.account_sid:
            raise DegradedCapability(
                "phone",
                "no Twilio credentials — NOVA_TWILIO_TOKEN in finance.env and the "
                "account SID in settings",
            )
        self._client = TwilioClient(settings.account_sid, self._token)

        self._server = await serve(
            self._on_stream,
            settings.host,
            settings.port,
            process_request=self._on_http,
            max_size=2 * 1024 * 1024,
        )
        self.log.info("phone_listening", host=settings.host, port=settings.port)

        self._public_url = settings.public_url.strip().rstrip("/")
        if not self._public_url:
            self._public_url = await self._open_tunnel(settings.port)
        if self._public_url and settings.answer_inbound:
            await self._register()

    async def on_stop(self) -> None:
        for task in tuple(self._active):
            task.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self._tunnel is not None:
            with contextlib.suppress(ProcessLookupError):
                self._tunnel.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._tunnel.wait(), timeout=5)
            self._tunnel = None

    def describe(self) -> str:
        where = self._public_url or "no public address"
        return f"{where} · {len(self._active)} call(s)"

    # ------------------------------------------------------------- the tunnel

    async def _open_tunnel(self, port: int) -> str:
        """Start cloudflared and read back the hostname it was handed.

        The free quick tunnel needs no account and no domain, which is the
        whole reason it is here — but the hostname is different every time, so
        it is read from the process rather than configured.
        """
        try:
            self._tunnel = await asyncio.create_subprocess_exec(
                "cloudflared",
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            self.log.warning(
                "tunnel_unavailable",
                remedy="install cloudflared, or set phone.public_url to a URL Twilio can reach",
            )
            return ""

        assert self._tunnel.stdout is not None
        try:
            async with asyncio.timeout(60):
                while True:
                    line = await self._tunnel.stdout.readline()
                    if not line:
                        break
                    found = _QUICK_TUNNEL.search(line.decode("utf-8", "replace"))
                    if found:
                        url = found.group(0)
                        self.log.info("tunnel_open", url=url)
                        self.spawn(self._drain_tunnel(), name="phone-tunnel")
                        return url
        except TimeoutError:
            self.log.warning("tunnel_timeout")
        return ""

    async def _drain_tunnel(self) -> None:
        """Keep reading cloudflared's output so its pipe never fills and stalls it."""
        if self._tunnel is None or self._tunnel.stdout is None:
            return
        while True:
            line = await self._tunnel.stdout.readline()
            if not line:
                return

    async def _register(self) -> None:
        if self._client is None or not self._public_url:
            return
        settings = self.ctx.settings.phone
        if not settings.from_number:
            self.log.warning("inbound_not_registered", reason="no from_number configured")
            return
        try:
            await self._client.point_at(settings.from_number, f"{self._public_url}/phone/answer")
        except SkillError as exc:
            self.log.warning("inbound_not_registered", error=str(exc.message))

    # ---------------------------------------------------------------- inbound

    async def _on_http(self, connection: ServerConnection, request: Request) -> Any:
        """Answer Twilio's webhook; let the media stream upgrade through."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None

        split = urlsplit(request.path)
        if split.path != "/phone/answer":
            return _text(404, "not found")

        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        if not self._authentic(request, params):
            self.log.warning("phone_request_rejected", reason="signature")
            return _text(403, "forbidden")

        intent = self._intents.get(params.get("intent", ""))
        if intent is None and not self.ctx.settings.phone.answer_inbound:
            return _twiml(twiml_reject("Sorry, I am not taking calls."))
        if intent is None and not self._from_owner(params):
            # An inbound call from a number that is not the owner's still gets
            # the PIN, so this only refuses when the setting says nobody else
            # may even reach the challenge.
            self.log.info("inbound_from_stranger")

        stream_url = f"{self._public_url.replace('https://', 'wss://')}/phone/stream"
        if params.get("intent"):
            stream_url += f"?intent={params['intent']}"
        return _twiml(twiml_connect(stream_url))

    def _authentic(self, request: Request, params: dict[str, str]) -> bool:
        provided = request.headers.get("X-Twilio-Signature", "")
        url = f"{self._public_url}{request.path}"
        # GET, so the signature covers the URL alone; the parameters are in it.
        return signature_matches(self._token, url, {}, provided) or signature_matches(
            self._token, url, params, provided
        )

    def _from_owner(self, params: dict[str, str]) -> bool:
        mine = self.ctx.settings.phone.my_number.strip()
        return bool(mine) and params.get("From", "").strip() == mine

    # ----------------------------------------------------------- the call itself

    async def _on_stream(self, connection: ServerConnection) -> None:
        split = urlsplit(connection.request.path if connection.request else "")
        token = parse_qs(split.query).get("intent", [""])[0]
        intent = self._intents.pop(token, None)

        stream = MediaStream(connection)
        call = self._build(stream, intent)

        reader = asyncio.create_task(stream.read(call.submit))
        self._active.add(reader)
        try:
            await call.run()
        finally:
            reader.cancel()
            self._active.discard(reader)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    def _build(self, stream: MediaStream, intent: Intent | None) -> Call:
        settings = self.ctx.settings.phone
        challenge = ""
        accepts = None
        if intent is None and settings.require_pin:
            pin = finance_secrets.load(self.ctx.paths.data_dir).get("NOVA_PHONE_PIN")
            if pin:
                challenge = "What is your PIN?"
                accepts = _pin_check(pin, stream)
            else:
                self.log.warning("inbound_pin_missing", remedy="set NOVA_PHONE_PIN in finance.env")

        return Call(
            transport=stream,
            transcribe=self._transcribe,
            synthesise=self._synthesise,
            respond=self._responder(intent),
            settings=settings,
            voice=self.ctx.settings.voice,
            opening=intent.opening if intent else "",
            challenge=challenge,
            accepts=accepts,
        )

    def _responder(self, intent: Intent | None) -> Any:
        """Answer the opener locally, then hand the rest to the model.

        The reply to "was this payment you" has to be read here. Sent to the
        model it arrives as a bare "no, that wasn't me" with no idea what
        question it answers, and the one thing that must not happen on a fraud
        check is a confident invented reply. Everything after that first
        exchange is an ordinary conversation and goes the ordinary way.
        """
        if intent is None or intent.first_reply is None:
            return self._respond

        answered = False

        async def respond(text: str) -> str:
            nonlocal answered
            if not answered:
                answered = True
                scripted = await intent.first_reply(text)
                if scripted:
                    return str(scripted)
            return await self._respond(text)

        return respond

    # ------------------------------------------------------- borrowed from voice

    async def _transcribe(self, audio: bytes) -> str:
        voice = self.ctx.service("voice")
        if voice is None or not voice.transcriber.loaded:
            return ""
        return (await voice.transcriber.transcribe(audio)).text

    async def _synthesise(self, text: str) -> np.ndarray | None:
        voice = self.ctx.service("voice")
        if voice is None or not voice.synthesiser.loaded:
            return None
        result = await voice.synthesiser.synthesise(text)
        if result is None:
            return None
        samples, rate = result
        # Kokoro's output is float32 at its own rate; the line wants int16 at 16 kHz.
        audio = np.asarray(samples, dtype=np.float32)
        if audio.size and np.abs(audio).max() <= 1.0:
            audio = audio * 32767.0
        if rate != SAMPLE_RATE and rate > 0:
            length = int(audio.size * SAMPLE_RATE / rate)
            audio = np.interp(np.linspace(0, audio.size - 1, length), np.arange(audio.size), audio)
        return np.clip(audio, -32768, 32767).astype(np.int16)

    async def _respond(self, text: str) -> str:
        orchestrator = self.ctx.service("orchestrator")
        if orchestrator is None:
            return "Sorry, I can't think straight just now."
        result = await orchestrator.handle(text, source="phone")
        return result.text or result.error or ""

    # --------------------------------------------------------------- outbound

    def may_call(self, now: datetime | None = None) -> bool:
        """Whether an unprompted call is acceptable at this hour.

        Quiet hours are not a nicety here. The whole point of the feature is a
        phone ringing without being asked, and one that does it at four in the
        morning gets switched off entirely.
        """
        settings = self.ctx.settings.phone
        start, until = _hhmm(settings.quiet_from), _hhmm(settings.quiet_until)
        if start is None or until is None:
            return True
        moment = (now or datetime.now()).time()
        inside = start <= moment < until if start <= until else (moment >= start or moment < until)
        return not inside

    async def call_owner(self, opening: str, *, first_reply: Any = None, **context: Any) -> str:
        """Ring the owner and open with `opening`. Returns the call SID."""
        settings = self.ctx.settings.phone
        if self._client is None or not self._public_url:
            raise SkillError("the phone is not connected")
        if not settings.my_number or not settings.from_number:
            raise SkillError("no number to call, or none to call from")

        token = secrets.token_urlsafe(12)
        self._intents[token] = Intent(
            opening=opening,
            created_at=asyncio.get_running_loop().time(),
            first_reply=first_reply,
            context=dict(context),
        )
        self._expire_intents()

        return await self._client.dial(
            to=settings.my_number,
            from_=settings.from_number,
            answer_url=f"{self._public_url}/phone/answer?intent={token}",
        )

    def _expire_intents(self) -> None:
        cutoff = asyncio.get_running_loop().time() - INTENT_TTL
        for token in [t for t, i in self._intents.items() if i.created_at < cutoff]:
            del self._intents[token]

    # ------------------------------------------------------------- large spend

    async def check_large_spend(
        self, merchant: str, amount: float, *, on_answer: Any = None
    ) -> bool:
        """Ring about one debit. False if the call was not placed.

        The opening line is built by `phrasing` from the transaction, not by a
        model — the same rule the rest of the finance module keeps.
        """
        settings = self.ctx.settings.phone
        if not settings.call_on_large_spend or abs(amount) < settings.call_threshold:
            return False
        if not self.may_call():
            self.log.info("call_suppressed", reason="quiet hours")
            return False

        opening = phrasing.large_spend_opening(
            merchant, amount, now=datetime.now(), honorific=settings.honorific
        )
        try:
            await self.call_owner(opening, first_reply=on_answer, merchant=merchant, amount=amount)
        except SkillError as exc:
            self.log.warning("large_spend_call_failed", error=str(exc.message))
            return False
        return True


def _pin_check(pin: str, stream: MediaStream) -> Any:
    """Accept the PIN spoken, or keyed on the handset.

    Keying it is the reliable path — a phone line and a transcriber together
    turn "one two three four" into most things — so the digits collected from
    DTMF are checked first and the spoken form is a fallback.
    """
    wanted = pin.strip()
    spoken = _spoken_digits(wanted)

    def accepts(answer: str) -> bool:
        keyed = "".join(stream.digits)
        if wanted and wanted in keyed:
            return True
        cleaned = answer.strip().lower().replace("-", " ").replace(",", "")
        return bool(wanted) and (wanted in cleaned.replace(" ", "") or spoken in cleaned)

    return accepts


_DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}


def _spoken_digits(pin: str) -> str:
    return " ".join(_DIGIT_WORDS.get(character, character) for character in pin)


def _hhmm(value: str) -> time | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        hour, _, minute = raw.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return None


def _twiml(xml: str) -> Response:
    headers = Headers()
    headers["Content-Type"] = "text/xml; charset=utf-8"
    headers["Content-Length"] = str(len(xml.encode()))
    return Response(200, "OK", headers, xml.encode())


def _text(status: int, body: str) -> Response:
    headers = Headers()
    headers["Content-Type"] = "text/plain; charset=utf-8"
    headers["Content-Length"] = str(len(body.encode()))
    return Response(status, "OK" if status < 400 else "Error", headers, body.encode())


__all__ = ["CallOutcome", "Ending", "Intent", "PhoneService"]
