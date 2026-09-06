"""AnthropicClient: OpenAI-shaped on both ends, Claude in the middle.

Two things matter most here and are easy to get wrong:

* The translation from OpenAI's chat-completions message/tool shape to
  Anthropic's Messages API — system lifted out, tool results folded into a
  user turn, tools renamed to ``input_schema``.
* That ``temperature`` never reaches the wire. Sonnet 5 rejects it with a 400,
  so the client accepts the argument for interface parity and must silently
  drop it.

A fake ``anthropic`` module stands in for the SDK, so none of this needs a key
or the network.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from nova.ai.anthropic_client import (
    AnthropicClient,
    _to_anthropic_messages,
    _to_anthropic_tool,
)

# ------------------------------------------------------------- translation


def test_system_messages_are_lifted_into_the_system_field() -> None:
    system, messages = _to_anthropic_messages(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
    )
    assert system == "You are helpful."
    assert messages == [{"role": "user", "content": "Hello"}]


def test_a_tool_call_and_its_result_translate_to_use_and_result_blocks() -> None:
    _, messages = _to_anthropic_messages(
        [
            {"role": "user", "content": "what's the time"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "clock", "arguments": '{"tz":"UTC"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "12:00"},
        ]
    )
    assert messages[1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_1", "name": "clock", "input": {"tz": "UTC"}}],
    }
    assert messages[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "12:00"}],
    }


def test_consecutive_tool_results_merge_into_one_user_turn() -> None:
    _, messages = _to_anthropic_messages(
        [
            {"role": "user", "content": "do two things"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a", "function": {"name": "one", "arguments": "{}"}},
                    {"id": "b", "function": {"name": "two", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "first"},
            {"role": "tool", "tool_call_id": "b", "content": "second"},
        ]
    )
    # Both results share a single user message, as Claude requires.
    assert messages[-1]["role"] == "user"
    assert [b["tool_use_id"] for b in messages[-1]["content"]] == ["a", "b"]


def test_a_leading_assistant_turn_is_dropped() -> None:
    # Claude requires the conversation to begin with the user.
    _, messages = _to_anthropic_messages(
        [
            {"role": "assistant", "content": "an earlier greeting"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert messages == [{"role": "user", "content": "hi"}]


def test_tool_definitions_are_renamed_to_input_schema() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    tool = _to_anthropic_tool(
        {
            "type": "function",
            "function": {"name": "do", "description": "does", "parameters": schema},
        }
    )
    assert tool == {"name": "do", "description": "does", "input_schema": schema}


# --------------------------------------------------------------- fake SDK


class _FakeMessages:
    def __init__(self, owner: _FakeAnthropic) -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> Any:
        self._owner.requests.append(kwargs)
        if kwargs.get("stream"):
            return _fake_event_stream()
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Here you go."),
                SimpleNamespace(type="tool_use", id="t1", name="lookup", input={"q": "x"}),
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


class _FakeAnthropic:
    instances: ClassVar[list[_FakeAnthropic]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requests: list[dict[str, Any]] = []
        self.messages = _FakeMessages(self)
        _FakeAnthropic.instances.append(self)

    async def close(self) -> None:  # pragma: no cover - not asserted
        pass


async def _fake_event_stream():
    # Mirrors Anthropic's SSE event objects closely enough for the reassembler:
    # a thinking-free text block, then a tool_use block whose JSON arrives split.
    yield SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(usage=SimpleNamespace(input_tokens=5)),
    )
    yield SimpleNamespace(
        type="content_block_start", index=0, content_block=SimpleNamespace(type="text")
    )
    yield SimpleNamespace(
        type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="Hello ")
    )
    yield SimpleNamespace(
        type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="there.")
    )
    yield SimpleNamespace(
        type="content_block_start",
        index=1,
        content_block=SimpleNamespace(type="tool_use", id="t9", name="search"),
    )
    yield SimpleNamespace(
        type="content_block_delta",
        index=1,
        delta=SimpleNamespace(type="input_json_delta", partial_json='{"query":'),
    )
    yield SimpleNamespace(
        type="content_block_delta",
        index=1,
        delta=SimpleNamespace(type="input_json_delta", partial_json='"cats"}'),
    )
    yield SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason="tool_use"),
        usage=SimpleNamespace(output_tokens=13),
    )


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAnthropic]:
    _FakeAnthropic.instances.clear()
    module = SimpleNamespace(AsyncAnthropic=_FakeAnthropic)
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return _FakeAnthropic


def make_client() -> AnthropicClient:
    return AnthropicClient(api_key="sk-test", model="claude-sonnet-5", timeout=30.0)


# ---------------------------------------------------------------- complete


async def test_complete_returns_text_and_tool_calls(fake_sdk: type[_FakeAnthropic]) -> None:
    client = make_client()
    completion = await client.complete(
        [{"role": "user", "content": "hi"}],
        model="claude-sonnet-5",
        temperature=0.9,
        max_tokens=1000,
    )
    assert completion.text == "Here you go."
    assert [c.name for c in completion.tool_calls] == ["lookup"]
    assert completion.tool_calls[0].parsed_arguments() == {"q": "x"}
    assert completion.prompt_tokens == 11 and completion.completion_tokens == 7


async def test_temperature_is_never_sent_to_claude(fake_sdk: type[_FakeAnthropic]) -> None:
    """Sonnet 5 rejects `temperature` with a 400 — the client must drop it."""
    client = make_client()
    await client.complete(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        model="claude-sonnet-5",
        temperature=0.9,
        max_tokens=500,
    )
    request = fake_sdk.instances[0].requests[0]
    assert "temperature" not in request
    assert "top_p" not in request
    assert request["system"] == "sys"
    assert request["max_tokens"] == 500


async def test_tools_are_translated_before_sending(fake_sdk: type[_FakeAnthropic]) -> None:
    client = make_client()
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    await client.complete(
        [{"role": "user", "content": "hi"}],
        model="claude-sonnet-5",
        tools=[
            {
                "type": "function",
                "function": {"name": "t", "description": "d", "parameters": schema},
            }
        ],
        max_tokens=500,
    )
    request = fake_sdk.instances[0].requests[0]
    assert request["tools"] == [{"name": "t", "description": "d", "input_schema": schema}]


def test_a_parameterless_tool_gets_a_valid_empty_object_schema() -> None:
    # Anthropic requires input_schema to be a schema object; an empty `{}` from
    # the registry is normalised rather than passed through and rejected.
    tool = _to_anthropic_tool({"type": "function", "function": {"name": "t", "parameters": {}}})
    assert tool["input_schema"] == {"type": "object", "properties": {}}


# ------------------------------------------------------------------ stream


async def test_stream_yields_text_deltas_then_a_reassembled_completion(
    fake_sdk: type[_FakeAnthropic],
) -> None:
    client = make_client()
    deltas: list[str] = []
    final = None
    async for delta, completion in client.stream(
        [{"role": "user", "content": "hi"}],
        model="claude-sonnet-5",
        temperature=0.7,
        max_tokens=1000,
    ):
        if completion is not None:
            final = completion
        elif delta:
            deltas.append(delta)

    assert "".join(deltas) == "Hello there."
    assert final is not None
    assert final.text == "Hello there."
    assert len(final.tool_calls) == 1
    call = final.tool_calls[0]
    assert call.name == "search"
    assert call.parsed_arguments() == {"query": "cats"}
    assert final.prompt_tokens == 5 and final.completion_tokens == 13


async def test_stream_also_omits_temperature(fake_sdk: type[_FakeAnthropic]) -> None:
    client = make_client()
    async for _ in client.stream(
        [{"role": "user", "content": "hi"}],
        model="claude-sonnet-5",
        temperature=0.7,
        max_tokens=100,
    ):
        pass
    request = fake_sdk.instances[0].requests[0]
    assert "temperature" not in request
    assert request["stream"] is True


# --------------------------------------------------------------- degrade


async def test_missing_key_raises_a_clear_error() -> None:
    client = AnthropicClient(api_key="", model="claude-sonnet-5")
    assert client.configured is False
    with pytest.raises(Exception, match="Anthropic API key"):
        await client.complete([{"role": "user", "content": "hi"}], model="claude-sonnet-5")


async def test_missing_dependency_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the SDK absent, the client raises MissingDependency rather than
    an opaque ImportError — the same graceful-degrade contract the rest of the
    app relies on."""
    from nova.runtime.errors import MissingDependency

    monkeypatch.setitem(sys.modules, "anthropic", None)
    client = make_client()
    with pytest.raises(MissingDependency):
        await client.complete([{"role": "user", "content": "hi"}], model="claude-sonnet-5")
