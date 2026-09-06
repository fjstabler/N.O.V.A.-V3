"""The things the finance module does without being asked.

Three of them, and each has a way of going wrong that is worse than not having
the feature: an alert that fires twice for one purchase, a cooling-off question
that gets marked answered without anyone hearing it, and a payday transfer that
runs more than once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nova.context import NovaContext
from nova.finance.ledger import Transaction
from nova.finance.service import FinanceService
from nova.notifications import NotificationService
from nova.runtime import Topics
from nova.runtime.service import ServiceState

STATEMENT = """Date,Counter Party,Amount (GBP),Balance (GBP)
13/09/2026,Employer Ltd,1000.00,1000.00
14/09/2026,Tesco,-42.50,957.50
"""


@pytest.fixture
def spoken(ctx: NovaContext) -> list[dict[str, Any]]:
    """Everything that actually reached a screen or a speaker."""
    said: list[dict[str, Any]] = []
    ctx.bus.subscribe(
        Topics.NOTIFICATION,
        lambda e: said.append(e.payload) if e.payload.get("_routed") else None,
    )
    return said


async def start_finance(ctx: NovaContext, tmp_path: Path, **overrides: Any) -> FinanceService:
    statement = tmp_path / "statement.csv"
    statement.write_text(STATEMENT, encoding="utf-8")
    ctx.store.patch(
        {
            "finance": {
                "enabled": True,
                "provider": "csv",
                "statement_path": str(statement),
                "large_spend_threshold": 100.0,
                **overrides,
            }
        },
        persist=False,
    )
    notifications = NotificationService(ctx)
    ctx.services.register(notifications)
    await notifications.start()

    service = FinanceService(ctx)
    ctx.services.register(service)
    await service.start()
    return service


def debit(amount: float, merchant: str = "Currys", *, ago: timedelta | None = None) -> Transaction:
    when = datetime.now(UTC) - (ago or timedelta())
    return Transaction(
        id=f"txn-{merchant}-{amount}",
        happened_at=when,
        amount=-abs(amount),
        merchant=merchant,
        source="webhook",
    )


def credit(amount: float, merchant: str = "Employer Ltd") -> Transaction:
    return Transaction(
        id=f"pay-{amount}",
        happened_at=datetime.now(UTC),
        amount=abs(amount),
        merchant=merchant,
        source="webhook",
    )


# ------------------------------------------------------------------ lifecycle


async def test_it_stays_out_of_the_way_when_finance_is_disabled(ctx: NovaContext) -> None:
    service = FinanceService(ctx)
    ctx.services.register(service)

    assert await service.start() is ServiceState.DEGRADED
    assert service.module is None


# ------------------------------------------------------------------- alerting


async def test_a_large_spend_is_announced_with_the_figures(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    service = await start_finance(ctx, tmp_path)

    await service._consider(debit(250, "Currys"), event_id="txn-1")

    assert len(spoken) == 1
    body = spoken[0]["body"]
    assert "£250" in body
    assert "Currys" in body
    assert "£957.50" in body, "what is left is the part that makes it an alert"
    await service.stop()


async def test_the_alert_offers_no_opinion(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    """Constraint 3 again, on the path where it is most tempting to break: the
    module has just watched somebody spend £250 and says only what happened."""
    service = await start_finance(ctx, tmp_path)

    await service._consider(debit(250, "Currys"), event_id="txn-1")

    said = f"{spoken[0]['title']} {spoken[0]['body']}".lower()
    for verdict in ("afford", "should", "careful", "too much", "sure", "really"):
        assert verdict not in said
    await service.stop()


async def test_a_replayed_delivery_only_alerts_once(
    ctx: NovaContext,
    tmp_path: Path,
    spoken: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bank retries until it gets a 2xx, so the second copy is ordinary
    traffic and must be silent.

    The notification service suppresses an identical panel raised within
    thirty seconds, which hides this: with that window in place the test
    passes whether or not the module deduplicates anything, and a retry an
    hour later would alert twice. Turned off here so the only thing that can
    keep it to one alert is the ledger's claim.
    """
    monkeypatch.setattr("nova.notifications.DEDUPE_WINDOW", 0.0)
    service = await start_finance(ctx, tmp_path)
    purchase = debit(250, "Currys")

    await service._consider(purchase, event_id=purchase.id)
    await service._consider(purchase, event_id=purchase.id)

    assert len(spoken) == 1
    await service.stop()


async def test_a_replayed_webhook_alerts_once_end_to_end(
    ctx: NovaContext,
    tmp_path: Path,
    spoken: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The brief's criterion, over the path a delivery actually takes: the same
    signed payload, parsed and handled twice."""
    from nova.finance.webhook import parse_event

    monkeypatch.setattr("nova.notifications.DEDUPE_WINDOW", 0.0)
    service = await start_finance(ctx, tmp_path)
    body = json.dumps(
        {
            "webhookNotificationUid": "delivery-1",
            "webhookType": "TRANSACTION_CARD",
            "content": {
                "transactionUid": "txn-1",
                "amount": 250.0,
                "direction": "OUT",
                "description": "Currys",
                "transactionTime": datetime.now(UTC).isoformat(),
            },
        }
    ).encode()

    await service._on_webhook(parse_event(body))
    # A retry carries a new delivery id for the same purchase.
    await service._on_webhook(parse_event(body.replace(b"delivery-1", b"delivery-2")))

    assert len(spoken) == 1
    assert service.module is not None
    stored = await service.module.ledger.spend_since(datetime.now(UTC) - timedelta(minutes=1))
    assert stored == 250.0, "and it is recorded once, not twice"
    await service.stop()


async def test_a_spend_under_the_threshold_says_nothing(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    service = await start_finance(ctx, tmp_path)

    await service._consider(debit(4.20, "Coffee"), event_id="txn-small")

    assert spoken == []
    await service.stop()


async def test_history_is_not_news(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    """The first poll after an upgrade sees a week of transactions. Announcing
    them would mean a restart replaying every large purchase of that week."""
    service = await start_finance(ctx, tmp_path)

    await service._consider(debit(250, "Currys", ago=timedelta(days=3)), event_id="old")

    assert spoken == []
    await service.stop()


async def test_a_webhook_without_a_transaction_is_ignored_quietly(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    from nova.finance.webhook import WebhookEvent

    service = await start_finance(ctx, tmp_path)

    await service._on_webhook(WebhookEvent(event_id="ping", kind="PING", transaction=None))

    assert spoken == []
    await service.stop()


# ---------------------------------------------------------------- cooling off


async def test_a_waiting_purchase_is_asked_about_once(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    service = await start_finance(ctx, tmp_path)
    assert service.module is not None
    await service.module.ledger.add_pending(
        "headphones", 200.0, datetime.now(UTC) - timedelta(hours=1)
    )

    await service._ask_what_is_due()
    await service._ask_what_is_due()

    assert len(spoken) == 1
    assert "headphones" in spoken[0]["body"]
    assert "£200" in spoken[0]["body"]
    await service.stop()


async def test_the_question_says_what_buying_it_would_leave(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    service = await start_finance(ctx, tmp_path)
    assert service.module is not None
    await service.module.ledger.add_pending(
        "headphones", 200.0, datetime.now(UTC) - timedelta(hours=1)
    )

    await service._ask_what_is_due()

    assert "would leave" in spoken[0]["body"]
    await service.stop()


async def test_a_question_nobody_heard_is_asked_again_later(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    """Do-not-disturb swallows the prompt. Marking it asked anyway would retire
    the one question the whole feature exists to deliver."""
    service = await start_finance(ctx, tmp_path)
    ctx.store.patch({"notifications": {"do_not_disturb": True}}, persist=False)
    assert service.module is not None
    await service.module.ledger.add_pending(
        "headphones", 200.0, datetime.now(UTC) - timedelta(hours=1)
    )

    await service._ask_what_is_due()

    assert spoken == []
    still = await service.module.ledger.pending()
    assert [p.item for p in still] == ["headphones"]
    assert still[0].asked_at is None, "not delivered is not asked"
    assert still[0].ask_after > datetime.now(UTC), "and it must not retry every minute"
    await service.stop()


# ------------------------------------------------------------------- payday


async def test_salary_runs_the_split_as_a_dry_run_by_default(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    service = await start_finance(
        ctx, tmp_path, transfer_amount=50.0, transfer_pot="Savings", transfer_max=100.0
    )
    assert service.module is not None

    await service._consider(credit(1500.0), event_id="pay-1")

    logged = await service.module.ledger.transfers()
    assert logged[0]["dry_run"] == 1, "nothing moves until both switches are set"
    assert "Dry run" in spoken[0]["body"]
    await service.stop()


async def test_the_split_runs_at_most_once_a_day(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    """An amended salary credit, or two payments on one day, must not move the
    money twice."""
    service = await start_finance(
        ctx, tmp_path, transfer_amount=50.0, transfer_pot="Savings", transfer_max=100.0
    )
    assert service.module is not None

    await service._consider(credit(1500.0), event_id="pay-1")
    await service._consider(credit(1600.0), event_id="pay-2")

    assert len(await service.module.ledger.transfers()) == 1
    await service.stop()


async def test_a_credit_too_small_to_be_salary_changes_nothing(
    ctx: NovaContext, tmp_path: Path
) -> None:
    service = await start_finance(
        ctx, tmp_path, transfer_amount=50.0, transfer_pot="Savings", salary_min=500.0
    )
    assert service.module is not None

    await service._consider(credit(20.0, "A refund"), event_id="refund-1")

    assert await service.module.ledger.transfers() == []
    await service.stop()


async def test_no_pot_configured_means_no_transfer_and_no_noise(
    ctx: NovaContext, tmp_path: Path, spoken: list[dict[str, Any]]
) -> None:
    service = await start_finance(ctx, tmp_path)
    assert service.module is not None

    await service._consider(credit(1500.0), event_id="pay-1")

    assert await service.module.ledger.transfers() == []
    assert spoken == []
    await service.stop()
