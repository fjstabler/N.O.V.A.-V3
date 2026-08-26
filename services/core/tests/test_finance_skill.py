"""Personal finance advisor: live Starling balance plus expiring upcoming-cost
reminders.

Every bank call here is a GET — these tests pin that down alongside the usual
success/error/degradation paths. Starling's HTTP is faked via
``httpx.MockTransport`` (matching ``test_osint_skill.py``); the upcoming-expense
memory tools run against a real ``MemoryService`` backed by a temp SQLite file
(matching ``test_home_skill.py``'s ``make_running_memory``), since the whole
point of the expiry behaviour is that it is real SQLite TTL logic, not a mock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nova.context import NovaContext
from nova.memory.models import MemoryKind
from nova.memory.service import MemoryService
from nova.runtime.errors import SkillError
from nova.skills.builtin.finance import FinanceSkill, _money

# --------------------------------------------------------------------- http


def patch_httpx(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Route every httpx.AsyncClient in the module under test through a MockTransport."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    original_init = httpx.AsyncClient.__init__

    def with_transport(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", with_transport)
    return requests


# ------------------------------------------------------------------- memory


async def make_running_memory(ctx: NovaContext) -> MemoryService:
    # Semantic recall would spawn a background model download; every test here
    # only ever needs the lexical path.
    ctx.store.patch({"memory": {"semantic_recall": False}}, persist=False)
    service = MemoryService(ctx)
    ctx.services.register(service)
    await service.start()
    return service


ACCOUNTS_RESPONSE = {
    "accounts": [
        {"accountUid": "acc-1", "defaultCategory": "cat-1", "name": "Personal", "currency": "GBP"}
    ]
}

BALANCE_RESPONSE = {
    "clearedBalance": {"currency": "GBP", "minorUnits": 150000},
    "effectiveBalance": {"currency": "GBP", "minorUnits": 142500},
}


def _feed_item(
    *, minor_units: int, direction: str = "OUT", status: str = "SETTLED"
) -> dict[str, Any]:
    return {
        "feedItemUid": "item-1",
        "amount": {"currency": "GBP", "minorUnits": minor_units},
        "direction": direction,
        "status": status,
        "reference": "TEST",
    }


def default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/accounts"):
        return httpx.Response(200, json=ACCOUNTS_RESPONSE)
    if path.endswith("/balance"):
        return httpx.Response(200, json=BALANCE_RESPONSE)
    if "/feed/" in path:
        return httpx.Response(
            200, json={"feedItems": [_feed_item(minor_units=2500), _feed_item(minor_units=1000)]}
        )
    if "standing-orders" in path:
        return httpx.Response(
            200,
            json={
                "standingOrders": [
                    {"amount": {"currency": "GBP", "minorUnits": 50000}, "reference": "Rent"}
                ]
            },
        )
    raise AssertionError(f"unexpected path {path}")


# ---------------------------------------------------------------- availability


def test_skill_is_unavailable_when_disabled(ctx: NovaContext) -> None:
    available, reason = FinanceSkill(ctx).is_available()
    assert available is False
    assert "disabled" in reason


def test_skill_is_unavailable_without_memory_service(ctx: NovaContext) -> None:
    ctx.store.patch({"finance": {"enabled": True}}, persist=False)
    available, reason = FinanceSkill(ctx).is_available()
    assert available is False
    assert "memory" in reason


async def test_skill_is_available_once_memory_is_running(ctx: NovaContext) -> None:
    ctx.store.patch({"finance": {"enabled": True}}, persist=False)
    await make_running_memory(ctx)
    available, reason = FinanceSkill(ctx).is_available()
    assert available is True
    assert reason == ""


def test_every_finance_tool_is_never_destructive(ctx: NovaContext) -> None:
    specs = {s.name: s for s in FinanceSkill(ctx).collect_tools()}
    assert set(specs) == {
        "check_balance",
        "check_affordability",
        "add_upcoming_expense",
        "list_upcoming_expenses",
        "cancel_upcoming_expense",
    }
    assert not any(s.destructive for s in specs.values())
    mutating = {name for name, spec in specs.items() if spec.mutating}
    assert mutating == {"add_upcoming_expense", "cancel_upcoming_expense"}


def test_context_lines_report_whether_a_bank_is_connected(ctx: NovaContext) -> None:
    assert "No bank account connected" in FinanceSkill(ctx).context_lines()[0]
    ctx.store.patch({"finance": {"starling_access_token": "tok"}}, persist=False)
    assert "connected" in FinanceSkill(ctx).context_lines()[0]


# ------------------------------------------------------------------- balance


async def test_check_balance_requires_a_token(ctx: NovaContext) -> None:
    with pytest.raises(SkillError, match="No Starling access token"):
        await FinanceSkill(ctx).check_balance()


async def test_check_balance_reports_cleared_and_effective(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"finance": {"starling_access_token": "tok"}}, persist=False)
    patch_httpx(monkeypatch, default_handler)

    reply = await FinanceSkill(ctx).check_balance()

    assert "Cleared balance: £1500.00." in reply
    assert "Effective balance once pending transactions settle: £1425.00." in reply


async def test_check_balance_omits_effective_when_it_matches_cleared(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"finance": {"starling_access_token": "tok"}}, persist=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json=ACCOUNTS_RESPONSE)
        return httpx.Response(
            200,
            json={
                "clearedBalance": {"currency": "GBP", "minorUnits": 100000},
                "effectiveBalance": {"currency": "GBP", "minorUnits": 100000},
            },
        )

    patch_httpx(monkeypatch, handler)

    reply = await FinanceSkill(ctx).check_balance()

    assert reply == "Cleared balance: £1000.00."


async def test_check_balance_rejects_a_bad_token(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"finance": {"starling_access_token": "wrong"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(401))

    with pytest.raises(SkillError, match="rejected the access token"):
        await FinanceSkill(ctx).check_balance()


async def test_check_balance_reports_no_accounts(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"finance": {"starling_access_token": "tok"}}, persist=False)
    patch_httpx(monkeypatch, lambda request: httpx.Response(200, json={"accounts": []}))

    with pytest.raises(SkillError, match="no accounts"):
        await FinanceSkill(ctx).check_balance()


async def test_check_balance_uses_the_named_account_when_configured(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch(
        {"finance": {"starling_access_token": "tok", "account_name": "Joint"}}, persist=False
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts"):
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {"accountUid": "acc-1", "defaultCategory": "cat-1", "name": "Personal"},
                        {"accountUid": "acc-2", "defaultCategory": "cat-2", "name": "Joint"},
                    ]
                },
            )
        assert "acc-2" in request.url.path
        return httpx.Response(200, json=BALANCE_RESPONSE)

    patch_httpx(monkeypatch, handler)

    await FinanceSkill(ctx).check_balance()


# -------------------------------------------------------------- affordability


async def test_check_affordability_without_a_bank_still_lists_upcoming_costs(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_running_memory(ctx)
    requests = patch_httpx(monkeypatch, default_handler)
    skill = FinanceSkill(ctx)
    await skill.add_upcoming_expense(description="council tax", amount=150.0, due="in 5 days")

    reply = await skill.check_affordability(item="a new phone", price=800.0)

    assert requests == []  # no Starling call at all without a token
    assert "No bank account connected" in reply
    assert "council tax: £150.00" in reply
    assert "cannot hold or block the purchase" in reply


async def test_check_affordability_combines_balance_spend_and_upcoming_costs(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"finance": {"starling_access_token": "tok"}}, persist=False)
    await make_running_memory(ctx)
    patch_httpx(monkeypatch, default_handler)
    skill = FinanceSkill(ctx)
    await skill.add_upcoming_expense(description="vet bill", amount=200.0, due="in 3 days")

    reply = await skill.check_affordability(item="a laptop", price=900.0)

    assert "Live balance right now (after pending transactions): £1425.00" in reply
    assert "Spent in the last 30 days: £35.00 across 2 transaction(s)" in reply
    assert "Standing orders currently set up: £500.00 total across 1 payment(s)" in reply
    assert "vet bill: £200.00" in reply
    # 1425.00 effective - 500 standing orders - 200 vet bill - 900 laptop = -175.00
    assert "£-175.00 left" in reply


async def test_check_affordability_degrades_gracefully_when_starling_errors(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"finance": {"starling_access_token": "tok"}}, persist=False)
    await make_running_memory(ctx)
    patch_httpx(monkeypatch, lambda request: httpx.Response(401))

    reply = await FinanceSkill(ctx).check_affordability(item="shoes", price=60.0)

    assert "Couldn't reach the live Starling balance" in reply
    assert "No upcoming one-off costs remembered." in reply


async def test_check_affordability_ignores_a_broken_standing_orders_endpoint(
    ctx: NovaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.store.patch({"finance": {"starling_access_token": "tok"}}, persist=False)
    await make_running_memory(ctx)

    def handler(request: httpx.Request) -> httpx.Response:
        if "standing-orders" in request.url.path:
            return httpx.Response(500)
        return default_handler(request)

    patch_httpx(monkeypatch, handler)

    reply = await FinanceSkill(ctx).check_affordability(item="shoes", price=60.0)

    assert "Standing orders" not in reply
    assert "Live balance right now" in reply  # rest of the check still ran


# ---------------------------------------------------------------- upcoming


async def test_add_and_list_upcoming_expense(ctx: NovaContext) -> None:
    await make_running_memory(ctx)
    skill = FinanceSkill(ctx)

    note = await skill.add_upcoming_expense(
        description="MOT", amount=45.0, due="in 2 days", notes="book ahead"
    )
    assert "MOT, £45.00" in note

    reply = await skill.list_upcoming_expenses()
    assert "MOT: £45.00" in reply


async def test_list_upcoming_expenses_sorts_by_due_date(ctx: NovaContext) -> None:
    await make_running_memory(ctx)
    skill = FinanceSkill(ctx)
    await skill.add_upcoming_expense(description="later bill", amount=10.0, due="in 20 days")
    await skill.add_upcoming_expense(description="sooner bill", amount=20.0, due="in 2 days")

    reply = await skill.list_upcoming_expenses()

    assert reply.index("sooner bill") < reply.index("later bill")


async def test_list_upcoming_expenses_reports_nothing_remembered(ctx: NovaContext) -> None:
    await make_running_memory(ctx)
    reply = await FinanceSkill(ctx).list_upcoming_expenses()
    assert reply == "Nothing remembered."


async def test_expired_upcoming_expense_stops_being_listed(ctx: NovaContext) -> None:
    memory = await make_running_memory(ctx)
    await memory.remember(
        "old bill — £10.00, due yesterday",
        kind=MemoryKind.EVENT,
        subject="upcoming-expense",
        source="explicit",
        metadata={"description": "old bill", "amount": 10.0, "due_at": time.time() - 86400},
        ttl_seconds=-1,  # already expired
    )

    reply = await FinanceSkill(ctx).list_upcoming_expenses()

    assert "old bill" not in reply


async def test_cancel_upcoming_expense_removes_a_fuzzy_match(ctx: NovaContext) -> None:
    await make_running_memory(ctx)
    skill = FinanceSkill(ctx)
    await skill.add_upcoming_expense(
        description="car insurance renewal", amount=300.0, due="in 7 days"
    )

    reply = await skill.cancel_upcoming_expense(description="car insurance")

    assert "Removed 'car insurance renewal'" in reply
    assert await skill.list_upcoming_expenses() == "Nothing remembered."


async def test_cancel_upcoming_expense_rejects_no_match(ctx: NovaContext) -> None:
    await make_running_memory(ctx)
    with pytest.raises(SkillError, match="Nothing upcoming matching"):
        await FinanceSkill(ctx).cancel_upcoming_expense(description="nonexistent")


async def test_cancel_upcoming_expense_rejects_an_empty_description(ctx: NovaContext) -> None:
    await make_running_memory(ctx)
    with pytest.raises(SkillError, match="Which upcoming cost"):
        await FinanceSkill(ctx).cancel_upcoming_expense(description="   ")


# ------------------------------------------------------------------ helpers


def test_money_converts_minor_units() -> None:
    assert _money({"currency": "GBP", "minorUnits": 12345}) == 123.45


def test_money_handles_none() -> None:
    assert _money(None) == 0.0
