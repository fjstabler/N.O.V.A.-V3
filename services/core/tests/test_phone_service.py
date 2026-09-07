"""The call that rings you, and what it does with the answer.

This is the only feature in N.O.V.A. that makes a phone ring without being
asked, so most of what is tested here is restraint: not at night, not below the
threshold, and not a confident invented reply to the one question where being
wrong matters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nova.context import NovaContext
from nova.finance.ledger import Transaction
from nova.finance.service import FinanceService
from nova.phone import phrasing
from nova.phone.service import PhoneService

STATEMENT = """Date,Counter Party,Amount (GBP),Balance (GBP)
13/09/2026,Employer Ltd,1000.00,1000.00
14/09/2026,Tesco,-42.50,957.50
"""


# ------------------------------------------------------------------ the opener


def test_the_opener_says_who_what_and_why_before_asking() -> None:
    """The brief's shape, near enough word for word. A phone ringing at nine in
    the evening that opens with "hello?" is a nuisance call from your own
    house."""
    said = phrasing.large_spend_opening(
        "Currys", -50.0, now=datetime(2026, 9, 14, 19, 30), honorific="sir"
    )

    assert said.startswith("Good evening sir")
    assert "50 pounds" in said or "£50" in said
    assert "Currys" in said
    assert said.rstrip().endswith(".")
    assert "check it was you" in said


@pytest.mark.parametrize(
    "hour,expected",
    [(7, "Good morning"), (11, "Good morning"), (13, "Good afternoon"), (20, "Good evening")],
)
def test_the_greeting_matches_the_clock(hour: int, expected: str) -> None:
    assert phrasing.greeting(datetime(2026, 9, 14, hour)).startswith(expected)


def test_the_honorific_is_optional() -> None:
    assert phrasing.greeting(datetime(2026, 9, 14, 19), "") == "Good evening"
    assert phrasing.greeting(datetime(2026, 9, 14, 19), "Finley") == "Good evening Finley"


# ------------------------------------------------------------------ the answer


@pytest.mark.parametrize(
    "answer", ["yes", "Yes.", "that was me", "yeah that was me", "yep", "I did", "that's mine"]
)
def test_an_affirmative_is_read_as_yes(answer: str) -> None:
    assert phrasing.reading(answer) == "yes"


@pytest.mark.parametrize(
    "answer", ["no", "No.", "that wasn't me", "nope", "not me", "I didn't", "that's not mine"]
)
def test_a_negative_is_read_as_no(answer: str) -> None:
    assert phrasing.reading(answer) == "no"


def test_no_that_was_me_is_not_read_as_no() -> None:
    """The trap, on the one question where being backwards matters most.
    Searching for the word "no" inside the answer gets this exactly wrong."""
    assert phrasing.reading("no that was me") == "unclear"


@pytest.mark.parametrize("answer", ["", "hang on", "what was the amount", "erm"])
def test_anything_else_is_unclear_rather_than_guessed(answer: str) -> None:
    assert phrasing.reading(answer) == "unclear"


def test_the_disputed_reply_does_not_pretend_to_have_frozen_anything() -> None:
    """The module is read-only apart from the payday transfer. Implying
    otherwise, at the moment somebody has just been told their card is being
    used by someone else, would be the worst possible time for it."""
    said = phrasing.disputed().lower()

    assert "starling" in said
    assert "can't do that part myself" in said
    for claim in ("i've frozen", "i have frozen", "blocked your card", "cancelled"):
        assert claim not in said


# ----------------------------------------------------------------- quiet hours


def phone(ctx: NovaContext, **overrides: Any) -> PhoneService:
    ctx.store.patch({"phone": {"enabled": True, **overrides}}, persist=False)
    return PhoneService(ctx)


@pytest.mark.parametrize("hour", [22, 23, 0, 3, 7])
def test_it_will_not_ring_you_in_the_night(ctx: NovaContext, hour: int) -> None:
    """The default window wraps midnight, which is the case a naive comparison
    gets wrong — and getting it wrong means a call at four in the morning, after
    which the feature is switched off and never used again."""
    service = phone(ctx, quiet_from="22:00", quiet_until="08:00")

    assert not service.may_call(datetime(2026, 9, 14, hour, 30))


@pytest.mark.parametrize("hour", [8, 12, 18, 21])
def test_it_will_ring_during_the_day(ctx: NovaContext, hour: int) -> None:
    service = phone(ctx, quiet_from="22:00", quiet_until="08:00")

    assert service.may_call(datetime(2026, 9, 14, hour, 0))


def test_clearing_the_window_allows_any_hour(ctx: NovaContext) -> None:
    service = phone(ctx, quiet_from="", quiet_until="")

    assert service.may_call(datetime(2026, 9, 14, 3, 0))


def test_the_phone_stays_out_of_the_way_when_it_is_off(ctx: NovaContext) -> None:
    ctx.store.patch({"phone": {"enabled": False}}, persist=False)
    service = PhoneService(ctx)

    assert service.describe().startswith("no public address")


# --------------------------------------------------------------- the verdict


async def finance(ctx: NovaContext, tmp_path: Path) -> FinanceService:
    statement = tmp_path / "statement.csv"
    statement.write_text(STATEMENT, encoding="utf-8")
    ctx.store.patch(
        {"finance": {"enabled": True, "provider": "csv", "statement_path": str(statement)}},
        persist=False,
    )
    service = FinanceService(ctx)
    if ctx.services.get("finance") is None:
        ctx.services.register(service)
    else:
        # A "restart" in a test reuses the manager; re-registering the same
        # name is refused, which is the manager doing its job.
        ctx.services._services["finance"] = service
    await service.start()
    return service


def debit(amount: float = 250.0, merchant: str = "Currys") -> Transaction:
    return Transaction(
        id="txn-1",
        happened_at=datetime.now(UTC),
        amount=-abs(amount),
        merchant=merchant,
        source="webhook",
    )


async def test_saying_it_was_not_you_records_a_dispute(ctx: NovaContext, tmp_path: Path) -> None:
    service = await finance(ctx, tmp_path)
    assert service.module is not None
    transaction = debit()
    await service.module.ledger.record_transactions([transaction])

    said = await service._read_verdict(transaction)("no that wasn't me")

    assert "Starling" in said
    disputed = await service.module.ledger.disputed()
    assert [row["id"] for row in disputed] == ["txn-1"]
    await service.stop()


async def test_saying_it_was_you_records_nothing(ctx: NovaContext, tmp_path: Path) -> None:
    service = await finance(ctx, tmp_path)
    assert service.module is not None
    transaction = debit()
    await service.module.ledger.record_transactions([transaction])

    said = await service._read_verdict(transaction)("yes that was me")

    assert "Thank you" in said
    assert await service.module.ledger.disputed() == []
    await service.stop()


async def test_an_unclear_answer_asks_again_rather_than_deciding(
    ctx: NovaContext, tmp_path: Path
) -> None:
    """A fraud check is the one exchange where a plausible guess is worst."""
    service = await finance(ctx, tmp_path)
    assert service.module is not None
    transaction = debit()
    await service.module.ledger.record_transactions([transaction])

    said = await service._read_verdict(transaction)("hang on, what was it for")

    assert said == phrasing.unclear()
    assert await service.module.ledger.disputed() == []
    await service.stop()


async def test_a_dispute_survives_a_restart(ctx: NovaContext, tmp_path: Path) -> None:
    """It is written to the ledger, not held in memory — the whole point is
    that it is still there when somebody rings the bank tomorrow."""
    service = await finance(ctx, tmp_path)
    assert service.module is not None
    transaction = debit()
    await service.module.ledger.record_transactions([transaction])
    await service.module.ledger.dispute(transaction.id)
    await service.stop()

    again = await finance(ctx, tmp_path)
    assert again.module is not None

    assert len(await again.module.ledger.disputed()) == 1
    await again.stop()


async def test_nothing_rings_when_the_phone_is_not_running(
    ctx: NovaContext, tmp_path: Path
) -> None:
    """The finance service must work perfectly well with no telephone at all —
    it is an addition, not a dependency."""
    service = await finance(ctx, tmp_path)
    assert service.module is not None

    await service._maybe_ring(debit())  # no phone service registered

    assert await service.module.ledger.disputed() == []
    await service.stop()


async def test_history_never_triggers_a_call(ctx: NovaContext, tmp_path: Path) -> None:
    """A restart must not ring you about last Tuesday's shopping."""
    service = await finance(ctx, tmp_path)
    assert service.module is not None
    old = Transaction(
        id="old",
        happened_at=datetime.now(UTC) - timedelta(days=3),
        amount=-500.0,
        merchant="Currys",
        source="csv",
    )

    rang: list[str] = []

    class FakePhone:
        name = "phone"
        running = True

        async def check_large_spend(self, merchant: str, amount: float, **_: Any) -> bool:
            rang.append(merchant)
            return True

    ctx.services.register(FakePhone())  # type: ignore[arg-type]

    await service._consider(old, event_id="old")

    assert rang == [], "a transaction older than the service start is not news"
    await service.stop()
