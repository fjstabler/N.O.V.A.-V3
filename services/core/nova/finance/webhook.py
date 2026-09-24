"""The bank's end of the connection: signed transaction webhooks.

A bank that can push a transaction the moment it happens is the difference
between an alert and a daily summary, so this exists — but it is the one place
in the module where something outside the house speaks first, and it is written
accordingly:

* a socket of its own, bound to loopback by default, so nothing is exposed by
  turning the feature on;
* one path, one method, a size cap, and a read timeout;
* the signature checked before the body is parsed, in constant time, and an
  unsigned or wrongly-signed delivery is dropped without ever becoming JSON;
* no credential in config — the shared secret comes from `finance.env`.

The server is written directly on `asyncio.start_server` rather than pulling in
a web framework. What it has to do is small and completely specified, and a
dependency added for one endpoint is a dependency the whole assistant then has.

A verified delivery is answered 200 before the work is done. The bank retries
until it gets a 2xx and a retry storm is worse than a missed alert — and
nothing is actually lost, because the poll loop sees the same transaction on
its next pass.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..runtime.logging import get_logger
from .ledger import Transaction

log = get_logger(__name__)

#: Nothing a bank sends about one transaction is anywhere near this large.
MAX_BODY = 64 * 1024

#: A delivery that has not finished arriving by now is not going to.
READ_TIMEOUT = 10.0

#: Headers banks put the signature in, in the order they are tried.
SIGNATURE_HEADERS = ("x-hook-signature", "x-monzo-signature", "x-signature")


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """One delivery, after parsing.

    `event_id` is what dedupe keys on. It prefers the transaction's own id over
    the delivery id: a bank that retries generates a fresh delivery id each
    time for the same purchase, so keying on the delivery would let a retry
    through as new.
    """

    event_id: str
    kind: str
    transaction: Transaction | None


Handler = Callable[[WebhookEvent], Awaitable[None]]


# --------------------------------------------------------------- signatures


def signature_matches(secret: str, body: bytes, provided: str, *, scheme: str) -> bool:
    """Constant-time check of a delivery signature.

    Two schemes, because the two banks worth supporting differ. Starling
    base64-encodes a SHA-512 of the secret concatenated with the payload;
    Monzo sends a hex HMAC-SHA256. Neither is negotiable at runtime — the
    scheme is configured, so a caller cannot downgrade the check by choosing
    the header it sends.
    """
    if not secret or not provided:
        return False

    if scheme == "starling":
        digest = hashlib.sha512(secret.encode("utf-8") + body).digest()
        expected = base64.b64encode(digest).decode("ascii")
    elif scheme == "hmac-sha256":
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    else:  # pragma: no cover - unreachable while the setting is a Literal
        log.warning("finance_webhook_unknown_scheme", scheme=scheme)
        return False

    return hmac.compare_digest(expected, provided.strip())


# ------------------------------------------------------------------ parsing


def parse_event(body: bytes) -> WebhookEvent:
    """Turn a verified delivery into a transaction, or into nothing.

    Written against Starling's shape and tolerant of the rest: a payload this
    does not recognise becomes an event with no transaction rather than an
    exception, because a bank adding a field is not an error and must not stop
    the ones it does understand from arriving.
    """
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return WebhookEvent(event_id=uuid.uuid4().hex, kind="unreadable", transaction=None)
    if not isinstance(payload, dict):
        return WebhookEvent(event_id=uuid.uuid4().hex, kind="unreadable", transaction=None)

    content = payload.get("content")
    content = content if isinstance(content, dict) else {}
    kind = str(payload.get("webhookType") or content.get("type") or "unknown")

    transaction = _transaction_from(payload, content)
    delivery = str(
        payload.get("webhookNotificationUid") or payload.get("webhookEventUid") or uuid.uuid4().hex
    )
    return WebhookEvent(
        event_id=transaction.id if transaction else delivery,
        kind=kind,
        transaction=transaction,
    )


def _transaction_from(payload: dict[str, Any], content: dict[str, Any]) -> Transaction | None:
    identifier = content.get("transactionUid") or content.get("feedItemUid") or payload.get("id")
    raw_amount = content.get("amount", content.get("sourceAmount"))
    if identifier is None or raw_amount is None:
        return None

    try:
        # Starling's feed API counts in minor units; its webhooks send a plain
        # decimal. Both shapes turn up, so accept a bare number and a
        # {minorUnits, currency} block.
        if isinstance(raw_amount, dict):
            amount = round(int(raw_amount.get("minorUnits", 0)) / 100.0, 2)
            currency = str(raw_amount.get("currency", "GBP"))
        else:
            amount = round(float(raw_amount), 2)
            currency = str(content.get("currency", "GBP"))
    except (TypeError, ValueError):
        return None

    # The feed reports a magnitude and a direction. Without applying the sign
    # here every outgoing reads as income and the whole module lies.
    if str(content.get("direction", "")).upper() == "OUT":
        amount = -abs(amount)

    return Transaction(
        id=str(identifier),
        happened_at=_moment(content.get("transactionTime") or payload.get("timestamp")),
        amount=amount,
        merchant=str(content.get("counterPartyName") or content.get("description") or ""),
        currency=currency,
        category=str(content.get("spendingCategory") or ""),
        source="webhook",
    )


def _moment(value: Any) -> datetime:
    if isinstance(value, str) and value:
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


# ------------------------------------------------------------------- server


class WebhookReceiver:
    """A one-endpoint HTTP server for signed transaction deliveries."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        path: str,
        secret: str,
        scheme: str,
        handler: Handler,
    ) -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self._secret = secret
        self._scheme = scheme
        self._handler = handler
        self._server: asyncio.Server | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        sockets = self._server.sockets or ()
        if sockets:
            # The port actually bound, which differs from the configured one
            # when that was 0. Reported rather than assumed, so the log line
            # says where the bank should post.
            self.port = int(sockets[0].getsockname()[1])
        log.info("finance_webhook_listening", host=self.host, port=self.port, path=self.path)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    # ------------------------------------------------------------ one request

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body = await asyncio.wait_for(self._respond(reader), timeout=READ_TIMEOUT)
        except TimeoutError:
            status, body = 408, b"timeout"
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the listener
            log.warning("finance_webhook_failed", error=str(exc)[:200])
            status, body = 400, b"bad request"

        with contextlib.suppress(Exception):
            writer.write(_response(status, body))
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

    async def _respond(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        request_line = (await reader.readline()).decode("latin-1").strip()
        parts = request_line.split()
        if len(parts) < 2:
            return 400, b"bad request"
        method, target = parts[0].upper(), parts[1]

        headers = await _read_headers(reader)

        if target.partition("?")[0] != self.path:
            return 404, b"not found"
        if method != "POST":
            # Answered explicitly so a browser pointed at the path gets a clear
            # refusal rather than a hang.
            return 405, b"method not allowed"

        length = _content_length(headers)
        if length is None:
            return 411, b"length required"
        if length > MAX_BODY:
            return 413, b"too large"

        body = await reader.readexactly(length) if length else b""

        provided = next(
            (headers[name] for name in SIGNATURE_HEADERS if name in headers),
            "",
        )
        if not signature_matches(self._secret, body, provided, scheme=self._scheme):
            # Deliberately terse and deliberately logged: this is the one thing
            # here worth noticing, and the body is not to be trusted enough to
            # quote back.
            log.warning("finance_webhook_rejected", reason="signature", bytes=len(body))
            return 401, b"unauthorized"

        event = parse_event(body)
        # Acknowledged first. The work happens after, on its own task, because
        # a bank that does not get a prompt 2xx retries — and a retry storm
        # costs more than the missed alert a failure here would cause, which
        # the poll loop picks up anyway.
        task = asyncio.create_task(self._dispatch(event))
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)
        return 200, b"ok"

    async def _dispatch(self, event: WebhookEvent) -> None:
        try:
            await self._handler(event)
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_webhook_handler_failed", kind=event.kind, error=str(exc)[:200])


#: Dispatch tasks, held so the loop does not garbage-collect one mid-flight.
_INFLIGHT: set[asyncio.Task[None]] = set()


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    for _ in range(64):  # a bank sends a dozen; the cap is against a hostile peer
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


def _content_length(headers: dict[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError:
        return None
    return length if length >= 0 else None


def _response(status: int, body: bytes) -> bytes:
    reason = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        408: "Request Timeout",
        411: "Length Required",
        413: "Payload Too Large",
    }.get(status, "Error")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode("latin-1") + body
