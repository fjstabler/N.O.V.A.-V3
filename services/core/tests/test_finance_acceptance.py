"""The brief's acceptance criteria, as tests.

These are not unit tests of a function. Each one is a rule the module has to
keep no matter how it is refactored later, and the first is the reason the
module was rewritten at all: an ordinary tool result goes back to the model
with the next request, so a finance tool that *returns* a balance is a finance
tool that puts that balance in a prompt.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from nova.context import NovaContext
from nova.finance.budget import Outgoing, assess
from nova.finance.module import FinanceModule
from nova.finance.phrasing import affordability
from nova.runtime.errors import FinalAnswer, SkillError
from nova.skills.builtin.finance import FinanceSkill

STATEMENT = """Date,Counter Party,Amount (GBP),Balance (GBP)
13/09/2026,Employer Ltd,1000.00,1000.00
14/09/2026,Tesco,-42.50,957.50
"""


def configure(ctx: NovaContext, tmp_path: Path, **overrides: Any) -> None:
    statement = tmp_path / "statement.csv"
    statement.write_text(STATEMENT, encoding="utf-8")
    ctx.store.patch(
        {
            "finance": {
                "enabled": True,
                "provider": "csv",
                "statement_path": str(statement),
                "payday_day": 25,
                "committed": [{"name": "phone", "amount": 60.0, "day_of_month": 20}],
                **overrides,
            }
        },
        persist=False,
    )


def make_module(ctx: NovaContext, tmp_path: Path) -> FinanceModule:
    return FinanceModule(ctx.settings.finance, tmp_path)


# ------------------------------------------------------- no data to a model


def test_every_finance_tool_answers_without_returning_anything() -> None:
    """The criterion the module exists for.

    A tool that returns a value has that value appended to the model's message
    list and sent with the next request. Every tool here must therefore end by
    raising `FinalAnswer` on its success path — checked by reading the source,
    because the alternative is trusting that nobody adds a `return` later.
    """
    tools = [
        getattr(FinanceSkill, name)
        for name in dir(FinanceSkill)
        if getattr(getattr(FinanceSkill, name, None), "__nova_tool__", None)
    ]
    assert tools, "the skill should expose tools"

    for handler in tools:
        source = inspect.getsource(handler)
        assert "raise FinalAnswer" in source, f"{handler.__name__} must raise FinalAnswer"
        body = source.split("\n", 1)[1]
        assert "\n        return " not in body, (
            f"{handler.__name__} returns a value, which would be sent to the model"
        )


async def test_a_finance_tool_raises_rather_than_returns(ctx: NovaContext, tmp_path: Path) -> None:
    """The behaviour behind the source check, exercised for real."""
    configure(ctx, tmp_path)
    skill = FinanceSkill(ctx)

    with pytest.raises(FinalAnswer) as raised:
        await skill.affordability(200.0)

    # The figures are in the answer, and the answer is the end of the turn.
    assert "available" in raised.value.text
    assert "£" in raised.value.text


async def test_the_registry_does_not_turn_the_answer_into_a_tool_result(
    ctx: NovaContext, tmp_path: Path
) -> None:
    """`_invoke` wraps stray exceptions in ToolExecutionError, whose message is
    then handed to the model as "Error: ...". If FinalAnswer were caught by
    that, the balance would reach a prompt by the back door."""
    from nova.skills.registry import SkillRegistry

    configure(ctx, tmp_path)
    registry = SkillRegistry(ctx)
    await registry.start()

    assert "finance_affordability" in {spec.qualified_name for spec in registry.tools}

    with pytest.raises(FinalAnswer):
        await registry.call("finance_affordability", {"amount": 0})

    await registry.stop()


def test_the_spoken_answer_is_built_from_numbers_not_by_a_model() -> None:
    """Determinism, which is also the proof no model is involved: a model
    called twice does not produce the same sentence."""
    result = assess(
        balance=957.50,
        outgoings=[Outgoing("phone", 60, 20)],
        today=date(2026, 9, 14),
        payday_day=25,
        spend=200.0,
    )
    assert len({affordability(result) for _ in range(20)}) == 1


# ------------------------------------------------------------- no verdicts


async def test_reporting_alone_offers_no_opinion(ctx: NovaContext, tmp_path: Path) -> None:
    """Constraint 3, which the owner has since reversed — so this is now the
    test for `advice: false`, the setting that puts the brief's behaviour back.
    With it off the module reports figures and stops, exactly as before."""
    configure(ctx, tmp_path, advice=False)
    module = make_module(ctx, tmp_path)
    await module.open()

    for spend in (0.0, 10.0, 100_000.0):
        said = (await module.affordability(spend)).lower()
        for verdict in ("yes", "afford", "should", "cannot", "too much", "sorry"):
            assert verdict not in said, f"{verdict!r} in {said!r}"


async def test_advice_recommends_without_forbidding(ctx: NovaContext, tmp_path: Path) -> None:
    """What replaced constraint 3, and the line that is still held.

    The module now says what it would do. What it must never do is claim
    authority it does not have: it cannot stop the purchase, the card will work
    regardless, and pretending otherwise is the thing that makes a limit into
    something to argue with rather than something to think about."""
    configure(ctx, tmp_path)
    module = make_module(ctx, tmp_path)
    await module.open()

    for amount, kind in ((5.0, "want"), (500.0, "want"), (500.0, "need"), (5000.0, "need")):
        said = (await module.advise("a thing", amount, kind)).lower()
        for forbidding in ("you can't", "you cannot", "not allowed", "don't buy", "you must"):
            assert forbidding not in said, f"{forbidding!r} in {said!r}"


async def test_the_advice_is_arithmetic_not_a_model(ctx: NovaContext, tmp_path: Path) -> None:
    """Twenty identical answers is not something a model produces."""
    configure(ctx, tmp_path)
    module = make_module(ctx, tmp_path)
    await module.open()

    said = {await module.advise("a Nintendo Switch", 300.0, "want") for _ in range(20)}

    assert len(said) == 1


def test_the_model_is_asked_about_the_item_and_nothing_else() -> None:
    """The split that lets advice exist without breaking constraint 1.

    The model judges whether the thing is a need or a want — a question about
    the item, answerable with no access to the account. Every figure stays on
    this machine. A parameter added here for a balance or a payday would make
    the model a party to the numbers, so the schema is pinned."""
    schema = FinanceSkill.should_i_buy.__nova_tool__  # type: ignore[attr-defined]
    assert schema  # the decorator ran

    from nova.skills.base import build_parameter_schema

    fields = set(build_parameter_schema(FinanceSkill.should_i_buy)["properties"])

    assert fields == {"item", "amount", "kind"}, (
        "the model must be asked for the item, its price and its necessity — nothing else"
    )


# ------------------------------------------------------------------ webhooks


async def test_replaying_a_webhook_alerts_once(ctx: NovaContext, tmp_path: Path) -> None:
    """Banks retry a delivery until it is acknowledged, so a second copy of the
    same transaction is normal traffic rather than an anomaly."""
    configure(ctx, tmp_path)
    module = make_module(ctx, tmp_path)
    await module.open()

    assert await module.ledger.claim_event("feed-item-1") is True
    assert await module.ledger.claim_event("feed-item-1") is False


# ----------------------------------------------------------------- transfers


async def test_transfers_do_not_execute_without_the_flag(ctx: NovaContext, tmp_path: Path) -> None:
    """`enable_transfers` defaults to false and nothing moves until it is set —
    the dry run records what it would have done and says so."""
    configure(
        ctx,
        tmp_path,
        transfer_amount=50.0,
        transfer_pot="Savings",
        transfer_max=100.0,
        enable_transfers=False,
    )
    module = make_module(ctx, tmp_path)
    await module.open()

    said = await module.payday_split()

    assert "Dry run" in said
    assert "not enabled" in said
    logged = await module.ledger.transfers()
    assert logged[0]["dry_run"] == 1


async def test_a_transfer_over_the_cap_is_refused_not_clamped(
    ctx: NovaContext, tmp_path: Path
) -> None:
    """The guard against a bug draining the account. Clamping to the cap would
    still move money nobody asked to move."""
    configure(
        ctx,
        tmp_path,
        transfer_amount=5000.0,
        transfer_pot="Savings",
        transfer_max=100.0,
        enable_transfers=True,
        transfer_dry_run=False,
    )
    module = make_module(ctx, tmp_path)
    await module.open()

    with pytest.raises(SkillError, match="cap"):
        await module.payday_split()

    assert await module.ledger.transfers() == []


async def test_dry_run_still_applies_when_transfers_are_enabled(
    ctx: NovaContext, tmp_path: Path
) -> None:
    """Two switches, deliberately: enabling transfers is not the same decision
    as turning off the rehearsal."""
    configure(
        ctx,
        tmp_path,
        transfer_amount=50.0,
        transfer_pot="Savings",
        transfer_max=100.0,
        enable_transfers=True,
        transfer_dry_run=True,
    )
    module = make_module(ctx, tmp_path)
    await module.open()

    said = await module.payday_split()

    assert "Dry run" in said
    assert "dry run is on" in said


# ------------------------------------------------------------------ secrets


def test_the_settings_panel_offers_finance_but_carries_no_credential() -> None:
    """The panel is generated from the schema, so a token added to the model
    would appear on every screen in the house without anyone deciding to put
    it there. Nothing in this section may be a secret, because nothing in this
    section is one."""
    from nova.config import describe_settings

    sections = {section["key"]: section for section in describe_settings()}
    assert "finance" in sections, "there is no way to turn it on from a wall panel"

    def walk(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for field in fields:
            found.append(field)
            found.extend(walk(field.get("fields", [])))
        return found

    for field in walk(sections["finance"]["fields"]):
        assert not field.get("secret"), f"finance.{field['key']} is marked secret"
        for banned in ("token", "secret", "password", "key"):
            assert banned not in field["key"], f"finance.{field['key']} looks like a credential"


def test_the_bank_token_is_not_a_setting(ctx: NovaContext) -> None:
    """Constraint 2. `config.toml` is written by the settings panel, sent to
    every client and restored from an export; a bank token belongs in none of
    those journeys."""
    finance = ctx.settings.finance.model_dump()

    for field in finance:
        assert "token" not in field, f"finance.{field} looks like a credential"
        assert "secret" not in field
        assert "key" not in field


async def test_a_missing_token_is_a_clear_error_not_a_crash(
    ctx: NovaContext, tmp_path: Path
) -> None:
    configure(ctx, tmp_path, provider="starling")
    module = make_module(ctx, tmp_path)
    await module.open()

    with pytest.raises(SkillError, match="NOVA_FINANCE_TOKEN"):
        await module.affordability()


# ------------------------------------------------------- the way back in


class RecordingMemory:
    """Stands in for the memory service, keeping what it was asked to store."""

    name = "memory"
    running = True

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.facts: list[str] = []

    async def recall(self, _text: str) -> list[Any]:
        return []

    async def working_context(self) -> list[Any]:
        return []

    async def record_turn(self, role: str, content: str, **_: Any) -> None:
        self.turns.append((role, content))

    async def remember(self, text: str, **_: Any) -> None:
        self.facts.append(text)


async def test_a_final_answer_is_not_written_into_the_conversation(ctx: NovaContext) -> None:
    """The second way a balance reaches a prompt, and the one that took longest
    to see.

    Keeping the answer out of the tool results is only half of it. The turn's
    reply is then recorded as conversation history and as a remembered fact,
    and history is sent with the *next* request — so one finance question
    followed by any other question puts the balance in a prompt after all. A
    phone call is a long multi-turn conversation, which is exactly where this
    would bite hardest.
    """
    from nova.ai.orchestrator import Orchestrator, TurnResult

    memory = RecordingMemory()
    ctx.services.register(memory)  # type: ignore[arg-type]
    orchestrator = Orchestrator(ctx)

    private = TurnResult(text="£340 available. 13 days until payday.", private=True)
    await orchestrator._remember_turn("how much have I got", private)

    assert memory.facts == [], "a balance must not become a remembered fact"
    assert "£340" not in " ".join(content for _, content in memory.turns)


async def test_an_ordinary_reply_is_still_remembered(ctx: NovaContext) -> None:
    """The fix must not cost the assistant its memory of everything else."""
    from nova.ai.orchestrator import Orchestrator, TurnResult

    memory = RecordingMemory()
    ctx.services.register(memory)  # type: ignore[arg-type]
    orchestrator = Orchestrator(ctx)

    await orchestrator._remember_turn("what's the weather", TurnResult(text="17 degrees and dry"))

    assert ("assistant", "17 degrees and dry") in memory.turns
    assert memory.facts, "ordinary exchanges are still worth keeping"
