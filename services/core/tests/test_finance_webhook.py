"""The one place in the finance module that something outside speaks first.

Everything else here is asked a question by its owner. This listens on a socket
for whatever arrives, so the tests are about refusal as much as about
delivery: an unsigned payload, a wrongly signed one, a huge one, one on the
wrong path, and one sent twice.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from typing import Any

import pytest

from nova.finance.webhook import (
    MAX_BODY,
    WebhookEvent,
    WebhookReceiver,
    parse_event,
    signature_matches,
)

SECRET = "a-shared-secret"

STARLING = {
    "webhookNotificationUid": "delivery-1",
    "timestamp": "2026-09-14T10:30:00.000Z",
    "webhookType": "TRANSACTION_CARD",
    "content": {
        "transactionUid": "txn-1",
        "amount": 42.50,
        "direction": "OUT",
        "description": "TESCO STORES",
        "transactionTime": "2026-09-14T10:29:58.000Z",
        "spendingCategory": "GROCERIES",
    },
}


def body_of(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def starling_signature(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hashlib.sha512(secret.encode() + body).digest()).decode()


# --------------------------------------------------------------- signatures


def test_a_correct_starling_signature_is_accepted() -> None:
    body = body_of(STARLING)

    assert signature_matches(SECRET, body, starling_signature(body), scheme="starling")


def test_a_signature_for_different_bytes_is_refused() -> None:
    """The whole point: the signature covers the body, so an attacker cannot
    keep a captured signature and change the amount."""
    signature = starling_signature(body_of(STARLING))
    tampered = body_of({**STARLING, "content": {**STARLING["content"], "amount": 4250.0}})

    assert not signature_matches(SECRET, tampered, signature, scheme="starling")


def test_a_signature_from_a_different_secret_is_refused() -> None:
    body = body_of(STARLING)

    assert not signature_matches(
        SECRET, body, starling_signature(body, "guessed"), scheme="starling"
    )


@pytest.mark.parametrize("secret,provided", [("", "abc"), (SECRET, ""), ("", "")])
def test_nothing_missing_is_ever_treated_as_a_match(secret: str, provided: str) -> None:
    """An empty secret must never make everything valid — that failure mode
    turns a misconfiguration into an open endpoint."""
    assert not signature_matches(secret, b"{}", provided, scheme="starling")


def test_the_hmac_scheme_is_the_other_shape() -> None:
    body = body_of(STARLING)
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert signature_matches(SECRET, body, expected, scheme="hmac-sha256")
    # And the schemes are not interchangeable.
    assert not signature_matches(SECRET, body, expected, scheme="starling")


def test_an_unknown_scheme_refuses_rather_than_falls_back() -> None:
    body = body_of(STARLING)

    assert not signature_matches(SECRET, body, starling_signature(body), scheme="whatever")


# ------------------------------------------------------------------ parsing


def test_a_card_payment_reads_as_money_going_out() -> None:
    event = parse_event(body_of(STARLING))

    assert event.transaction is not None
    assert event.transaction.amount == -42.50, "OUT must be negative or every debit reads as income"
    assert event.transaction.is_debit
    assert event.transaction.merchant == "TESCO STORES"
    assert event.transaction.source == "webhook"


def test_money_coming_in_stays_positive() -> None:
    payload = {**STARLING, "content": {**STARLING["content"], "direction": "IN", "amount": 1000.0}}

    event = parse_event(body_of(payload))

    assert event.transaction is not None
    assert event.transaction.amount == 1000.0


def test_minor_units_are_understood_too() -> None:
    """Starling's feed API counts in pence and its webhooks send decimals.
    Both shapes turn up depending on which product sent it."""
    payload = {
        **STARLING,
        "content": {**STARLING["content"], "amount": {"minorUnits": 4250, "currency": "GBP"}},
    }

    event = parse_event(body_of(payload))

    assert event.transaction is not None
    assert event.transaction.amount == -42.50


def test_the_event_id_is_the_transaction_not_the_delivery() -> None:
    """A retry carries a fresh delivery id for the same purchase. Keying dedupe
    on the delivery would let every retry through as a new alert."""
    first = parse_event(body_of(STARLING))
    retry = parse_event(body_of({**STARLING, "webhookNotificationUid": "delivery-2"}))

    assert first.event_id == retry.event_id == "txn-1"


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'{"webhookType": "PING"}', b"\xff\xfe"],
)
def test_anything_unrecognised_becomes_no_transaction_rather_than_an_error(body: bytes) -> None:
    """A bank adding a field or sending a keepalive must not stop the
    deliveries that do carry a transaction."""
    event = parse_event(body)

    assert isinstance(event, WebhookEvent)
    assert event.transaction is None


# ------------------------------------------------------------------- server


class Caught:
    """Collects what the receiver hands on, and lets a test wait for it."""

    def __init__(self) -> None:
        self.events: list[WebhookEvent] = []
        self.arrived = asyncio.Event()

    async def __call__(self, event: WebhookEvent) -> None:
        self.events.append(event)
        self.arrived.set()


@pytest.fixture
async def receiver() -> WebhookReceiver:
    caught = Caught()
    server = WebhookReceiver(
        host="127.0.0.1",
        port=0,
        path="/finance/webhook",
        secret=SECRET,
        scheme="starling",
        handler=caught,
    )
    server.caught = caught  # type: ignore[attr-defined]
    await server.start()
    yield server
    await server.stop()


async def send(
    server: WebhookReceiver,
    *,
    method: str = "POST",
    target: str | None = None,
    body: bytes = b"",
    signature: str | None = None,
    content_length: bool = True,
) -> int:
    """One raw HTTP request, because a bank is not an httpx client."""
    reader, writer = await asyncio.open_connection(server.host, server.port)
    lines = [f"{method} {target or server.path} HTTP/1.1", "Host: nova"]
    if content_length:
        lines.append(f"Content-Length: {len(body)}")
    if signature is not None:
        lines.append(f"X-Hook-Signature: {signature}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    await writer.drain()

    status_line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    return int(status_line.split()[1])


async def test_a_signed_delivery_is_accepted_and_handed_on(receiver: WebhookReceiver) -> None:
    body = body_of(STARLING)

    status = await send(receiver, body=body, signature=starling_signature(body))

    assert status == 200
    await asyncio.wait_for(receiver.caught.arrived.wait(), timeout=5)  # type: ignore[attr-defined]
    assert receiver.caught.events[0].event_id == "txn-1"  # type: ignore[attr-defined]


async def test_an_unsigned_delivery_never_reaches_the_handler(receiver: WebhookReceiver) -> None:
    status = await send(receiver, body=body_of(STARLING))

    assert status == 401
    await asyncio.sleep(0.05)
    assert receiver.caught.events == []  # type: ignore[attr-defined]


async def test_a_wrongly_signed_delivery_never_reaches_the_handler(
    receiver: WebhookReceiver,
) -> None:
    status = await send(receiver, body=body_of(STARLING), signature=starling_signature(b"{}"))

    assert status == 401
    await asyncio.sleep(0.05)
    assert receiver.caught.events == []  # type: ignore[attr-defined]


async def test_only_the_configured_path_answers(receiver: WebhookReceiver) -> None:
    body = body_of(STARLING)

    status = await send(receiver, target="/", body=body, signature=starling_signature(body))

    assert status == 404


async def test_a_browser_visiting_the_path_is_refused_not_hung(receiver: WebhookReceiver) -> None:
    assert await send(receiver, method="GET") == 405


async def test_an_oversized_body_is_refused_before_it_is_read(receiver: WebhookReceiver) -> None:
    """The size is taken from the header and refused there — reading a
    gigabyte to discover it is a gigabyte is the bug this avoids."""
    reader, writer = await asyncio.open_connection(receiver.host, receiver.port)
    writer.write(
        f"POST {receiver.path} HTTP/1.1\r\nContent-Length: {MAX_BODY + 1}\r\n\r\n".encode()
    )
    await writer.drain()
    status = int((await asyncio.wait_for(reader.readline(), timeout=5)).split()[1])
    writer.close()

    assert status == 413


async def test_a_body_with_no_length_is_refused(receiver: WebhookReceiver) -> None:
    status = await send(receiver, body=b"{}", content_length=False, signature="x")

    assert status == 411
