"""Orchestrator tier routing: the right model handles the turn.

Everyday commands run on the OpenAI client; coding / system / complex requests
escalate to the Anthropic one — but only when an Anthropic key is set. These
drive a whole turn with both clients replaced by recorders, so the assertion is
about which backend actually ran, not about the router in isolation (that lives
in test_router.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.ai.client import Completion, ReasoningUnavailable
from nova.ai.orchestrator import Orchestrator
from nova.context import NovaContext


class FakeReasoner:
    """Stands in for either reasoning client, recording which path ran."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.complete_calls = 0
        self.stream_calls = 0

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Completion:
        self.complete_calls += 1
        return Completion(text=self.reply)

    async def stream(self, messages: list[dict[str, Any]], **kwargs: Any):
        self.stream_calls += 1
        yield "", Completion(text=self.reply)

    async def close(self) -> None:  # pragma: no cover - not asserted
        pass


def make_orchestrator(ctx: NovaContext) -> tuple[Orchestrator, FakeReasoner, FakeReasoner]:
    orchestrator = Orchestrator(ctx)
    basic = FakeReasoner("basic reply")
    advanced = FakeReasoner("advanced reply")
    orchestrator.client = basic  # type: ignore[assignment]
    orchestrator.advanced = advanced  # type: ignore[assignment]
    return orchestrator, basic, advanced


def configure(ctx: NovaContext, *, openai: bool = False, anthropic: bool = False) -> None:
    patch: dict[str, Any] = {}
    if openai:
        patch["openai"] = {"api_key": "sk-openai"}
    if anthropic:
        patch["anthropic"] = {"api_key": "sk-anthropic"}
    if patch:
        ctx.store.patch(patch, persist=False)


async def test_a_coding_request_routes_to_the_advanced_client(ctx: NovaContext) -> None:
    configure(ctx, openai=True, anthropic=True)
    orchestrator, basic, advanced = make_orchestrator(ctx)

    result = await orchestrator.handle("write a python function to sort a list", source="text")

    assert not result.error
    assert advanced.stream_calls == 1
    assert basic.stream_calls == 0
    assert orchestrator._turn_backend == "advanced"


async def test_an_everyday_command_stays_on_the_basic_client(ctx: NovaContext) -> None:
    configure(ctx, openai=True, anthropic=True)
    orchestrator, basic, advanced = make_orchestrator(ctx)

    result = await orchestrator.handle("what's the weather like", source="text")

    assert not result.error
    assert basic.stream_calls == 1
    assert advanced.stream_calls == 0
    assert orchestrator._turn_backend == "basic"


async def test_without_an_anthropic_key_even_coding_stays_on_openai(ctx: NovaContext) -> None:
    configure(ctx, openai=True, anthropic=False)
    orchestrator, basic, advanced = make_orchestrator(ctx)

    result = await orchestrator.handle("debug this python stack trace", source="text")

    assert not result.error
    assert basic.stream_calls == 1
    assert advanced.stream_calls == 0


async def test_with_only_anthropic_configured_everyday_commands_use_it_too(
    ctx: NovaContext,
) -> None:
    """Better to answer on the advanced tier than to refuse for want of the
    other one."""
    configure(ctx, openai=False, anthropic=True)
    orchestrator, basic, advanced = make_orchestrator(ctx)

    result = await orchestrator.handle("what's the weather like", source="text")

    assert not result.error
    assert advanced.stream_calls == 1
    assert basic.stream_calls == 0


async def test_select_backend_raises_when_neither_tier_is_configured(ctx: NovaContext) -> None:
    orchestrator, _, _ = make_orchestrator(ctx)
    with pytest.raises(ReasoningUnavailable, match="reasoning model"):
        orchestrator._select_backend("anything at all")


async def test_an_explicit_ask_escalates_even_with_auto_route_off(ctx: NovaContext) -> None:
    configure(ctx, openai=True, anthropic=True)
    ctx.store.patch({"anthropic": {"auto_route": False}}, persist=False)
    orchestrator, _basic, advanced = make_orchestrator(ctx)

    result = await orchestrator.handle("think hard about the weather", source="text")

    assert not result.error
    assert advanced.stream_calls == 1
