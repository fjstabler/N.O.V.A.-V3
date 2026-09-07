"""Twilio: placing calls, answering them, and proving a request is theirs.

Three separate things live here because they share nothing but a credential:

* the REST calls that dial a number and keep the webhook URL current;
* the TwiML returned when a call connects, which is how Twilio is told to open
  a media stream back to this machine;
* signature verification, so the endpoint answers Twilio and not whoever else
  finds the URL.

The auth token is never a setting. It sits in `finance.env` beside the bank
token, for the same reason: possession of it is possession of the ability to
make calls billed to somebody's card.

The webhook URL is re-registered on every start rather than configured once.
Without a domain the tunnel hands out a different hostname each time it comes
up, and a URL typed into a console by hand is stale by the next reboot — so
N.O.V.A. tells Twilio where it is instead of being told.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import quote, urlencode

from ..runtime.errors import SkillError
from ..runtime.logging import get_logger

log = get_logger(__name__)

API_ROOT = "https://api.twilio.com/2010-04-01"

#: How long to wait on Twilio's API. Dialling is not something to hang on.
TIMEOUT = 20.0


def twiml_connect(stream_url: str) -> str:
    """The XML that turns an answered call into a two-way audio stream.

    `<Connect>` rather than `<Start>`: the latter forks a copy of the caller's
    audio to a socket and is one-way, which would let N.O.V.A. listen and never
    reply. The difference is one word and total.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{_xml_escape(stream_url)}" />'
        "</Connect></Response>"
    )


def twiml_reject(reason: str = "") -> str:
    """Refuse a call politely. Used when the caller is not the owner."""
    spoken = _xml_escape(reason) if reason else "Sorry, I cannot take this call."
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{spoken}</Say><Hangup /></Response>'
    )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# ------------------------------------------------------------------ signature


def signature_matches(token: str, url: str, params: dict[str, str], provided: str) -> bool:
    """Verify `X-Twilio-Signature`.

    Twilio signs the full URL with the POST parameters appended, each as key
    then value, in sorted order — for a GET there are none and the URL alone is
    signed. HMAC-SHA1, base64, compared in constant time.

    SHA-1 because that is what Twilio uses. It is not a choice available here,
    and for message authentication with a shared secret it remains sound; the
    breaks against SHA-1 are collision attacks, which is a different problem.
    """
    if not token or not provided:
        return False

    payload = url
    for key in sorted(params):
        payload += key + params[key]

    digest = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, provided.strip())


class TwilioClient:
    """The few REST calls this needs, and nothing else."""

    def __init__(self, account_sid: str, token: str) -> None:
        if not account_sid or not token:
            raise SkillError(
                "no Twilio credentials. Put NOVA_TWILIO_TOKEN in finance.env and the "
                "account SID in the Telephone settings."
            )
        self._sid = account_sid
        self._token = token

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        return await self._request("POST", path, data=data)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a hard dependency
            raise SkillError("httpx is required to reach Twilio") from exc

        url = f"{API_ROOT}/Accounts/{quote(self._sid)}{path}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.request(
                    method,
                    url,
                    auth=(self._sid, self._token),
                    data=data,
                    params=params,
                    headers={"User-Agent": "nova-phone"},
                )
        except httpx.HTTPError as exc:
            raise SkillError(f"could not reach Twilio: {exc}") from exc

        if response.status_code == 401:
            raise SkillError("Twilio rejected the credentials. Check NOVA_TWILIO_TOKEN.")
        if response.status_code >= 400:
            # Twilio's error bodies name the account and the number; neither
            # belongs in a log file or on a screen.
            raise SkillError(f"Twilio returned {response.status_code}")
        try:
            return dict(response.json())
        except ValueError as exc:
            raise SkillError("Twilio sent something that was not JSON") from exc

    # -------------------------------------------------------------- outbound

    async def dial(self, *, to: str, from_: str, answer_url: str) -> str:
        """Place a call. Returns the call SID.

        `answer_url` is fetched by Twilio the moment the call is answered, and
        whatever TwiML it returns is what happens next.
        """
        payload = await self._post(
            "/Calls.json",
            {"To": to, "From": from_, "Url": answer_url, "Method": "GET"},
        )
        call_sid = str(payload.get("sid", ""))
        log.info("call_placed", to=_masked(to))
        return call_sid

    async def hang_up(self, call_sid: str) -> None:
        await self._post(f"/Calls/{quote(call_sid)}.json", {"Status": "completed"})

    # ------------------------------------------------------------- the number

    async def number_sid(self, number: str) -> str:
        """Twilio's own id for a number we own, needed to reconfigure it."""
        payload = await self._get("/IncomingPhoneNumbers.json", {"PhoneNumber": number})
        numbers = payload.get("incoming_phone_numbers") or []
        if not numbers:
            raise SkillError(f"this account does not own {number}")
        return str(numbers[0]["sid"])

    async def point_at(self, number: str, answer_url: str) -> None:
        """Tell Twilio where to send incoming calls.

        Called at every start. The tunnel's hostname changes whenever it
        restarts, and a URL configured by hand in the console is wrong from the
        next reboot onwards — silently, since an inbound call simply fails.
        """
        sid = await self.number_sid(number)
        await self._post(
            f"/IncomingPhoneNumbers/{quote(sid)}.json",
            {"VoiceUrl": answer_url, "VoiceMethod": "GET"},
        )
        log.info("inbound_url_registered", number=_masked(number))


def _masked(number: str) -> str:
    """`+44 ... 123` — enough to tell which number, not enough to dial it."""
    digits = number.strip()
    return f"{digits[:3]}...{digits[-3:]}" if len(digits) > 6 else "..."


def query_string(params: dict[str, str]) -> str:
    return urlencode(sorted(params.items()))
