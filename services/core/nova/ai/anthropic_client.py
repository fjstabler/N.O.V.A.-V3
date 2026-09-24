"""Anthropic (Claude) client — the advanced reasoning tier.

Deliberately a drop-in for :class:`nova.ai.client.OpenAIClient`: same
``complete`` / ``stream`` / ``close`` surface, the same
:class:`~nova.ai.client.Completion` and :class:`~nova.ai.client.ToolCall`
results, so the orchestrator can pick a backend per turn without knowing which
vendor is behind it. The one thing this file owns that the OpenAI one does not
is *translation* — the orchestrator speaks OpenAI's chat-completions shape
throughout (system/user/assistant/tool messages, ``{"type":"function",...}``
tools), and this converts to and from Anthropic's Messages API on the way in
and out.

Two Claude-specific details that would otherwise bite:

* **No sampling parameters.** Sonnet 5 (and the rest of the current Claude
  line) rejects ``temperature`` / ``top_p`` with a 400. The ``complete`` /
  ``stream`` signatures still accept a ``temperature`` for interface parity
  with the OpenAI client, and deliberately drop it on the floor.
* **``system`` is its own field**, not a message with ``role: "system"`` — the
  translator lifts every system message out into it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import AsyncIterator
from typing import Any

from ..runtime.errors import MissingDependency, NovaError
from ..runtime.logging import get_logger
from .client import _RETRY_STATUS, MAX_ATTEMPTS, Completion, ReasoningUnavailable, ToolCall

log = get_logger(__name__)


class AnthropicClient:
    """Async Claude client with retry and streaming, OpenAI-shaped on both ends."""

    def __init__(self, *, api_key: str, model: str, timeout: float = 120.0) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._client: Any = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def model(self) -> str:
        return self._model

    def reconfigure(self, *, api_key: str, model: str, timeout: float = 120.0) -> None:
        """Swap credentials/model without restarting the process."""
        if (api_key, model, timeout) == (self._api_key, self._model, self._timeout):
            return
        self._api_key, self._model, self._timeout = api_key, model, timeout
        self._client = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ReasoningUnavailable(
                "no Anthropic API key configured — add one in settings to enable advanced reasoning"
            )
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise MissingDependency("advanced reasoning", "anthropic", "ai") from exc

        self._client = AsyncAnthropic(api_key=self._api_key, timeout=self._timeout)
        return self._client

    # ----------------------------------------------------------- completions

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        max_tokens: int = 1024,
    ) -> Completion:
        """Non-streaming completion; used for tool-selection rounds."""
        client = self._ensure_client()
        request = self._build_request(messages, tools, model=model, max_tokens=max_tokens)
        message = await self._with_retry(lambda: client.messages.create(**request))
        return self._to_completion(message)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        max_tokens: int = 1024,
    ) -> AsyncIterator[tuple[str, Completion | None]]:
        """Yield ``(text_delta, None)`` while streaming, then ``("", completion)``.

        Text deltas stream straight to speech; tool-call arguments arrive as
        JSON fragments across ``input_json_delta`` events and are reassembled by
        block index before the final completion is emitted — the same shape the
        OpenAI client produces, so the orchestrator's streaming path is identical
        for both.
        """
        client = self._ensure_client()
        request = self._build_request(messages, tools, model=model, max_tokens=max_tokens)
        stream = await self._with_retry(lambda: client.messages.create(**request, stream=True))

        completion = Completion()
        # index -> {"type", "id", "name", "json"} for tool_use blocks under way.
        blocks: dict[int, dict[str, str]] = {}

        async for event in stream:
            etype = getattr(event, "type", "")
            if etype == "message_start":
                usage = getattr(getattr(event, "message", None), "usage", None)
                if usage is not None:
                    completion.prompt_tokens = getattr(usage, "input_tokens", 0) or 0
            elif etype == "content_block_start":
                cb = event.content_block
                if getattr(cb, "type", None) == "tool_use":
                    blocks[event.index] = {"id": cb.id, "name": cb.name, "json": ""}
            elif etype == "content_block_delta":
                delta = event.delta
                dtype = getattr(delta, "type", None)
                if dtype == "text_delta":
                    completion.text += delta.text
                    yield delta.text, None
                elif dtype == "input_json_delta":
                    slot = blocks.get(event.index)
                    if slot is not None:
                        slot["json"] += delta.partial_json
            elif etype == "message_delta":
                stop_reason = getattr(getattr(event, "delta", None), "stop_reason", None)
                if stop_reason:
                    completion.finish_reason = stop_reason
                usage = getattr(event, "usage", None)
                if usage is not None and getattr(usage, "output_tokens", None):
                    completion.completion_tokens = usage.output_tokens

        completion.text = completion.text.strip()
        for _, slot in sorted(blocks.items()):
            if slot.get("name"):
                completion.tool_calls.append(
                    ToolCall(id=slot["id"], name=slot["name"], arguments=slot["json"] or "{}")
                )
        yield "", completion

    # ------------------------------------------------------------ translation

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        system, translated = _to_anthropic_messages(messages)
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": translated,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [_to_anthropic_tool(t) for t in tools]
        return request

    def _to_completion(self, message: Any) -> Completion:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in getattr(message, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=json.dumps(block.input or {}))
                )
            # thinking / other block types are not part of the visible reply.

        text = "".join(text_parts).strip()
        stop_reason = getattr(message, "stop_reason", "") or ""
        if stop_reason == "refusal" and not text:
            text = "I'm not able to help with that one."

        completion = Completion(text=text, tool_calls=tool_calls, finish_reason=stop_reason)
        usage = getattr(message, "usage", None)
        if usage is not None:
            completion.prompt_tokens = getattr(usage, "input_tokens", 0) or 0
            completion.completion_tokens = getattr(usage, "output_tokens", 0) or 0
        return completion

    # ------------------------------------------------------------------ retry

    async def _with_retry(self, factory: Any) -> Any:
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await factory()
            except Exception as exc:
                last = exc
                status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                if status is not None and status not in _RETRY_STATUS:
                    raise self._normalise(exc, status) from exc
                if attempt == MAX_ATTEMPTS - 1:
                    break
                delay = (2**attempt) + random.uniform(0, 0.5)
                log.warning(
                    "anthropic_retry",
                    attempt=attempt + 1,
                    delay=round(delay, 2),
                    error=str(exc)[:200],
                )
                await asyncio.sleep(delay)
        raise self._normalise(last, getattr(last, "status_code", None)) from last

    def _normalise(self, exc: Exception | None, status: Any) -> NovaError:
        text = str(exc) if exc else "unknown error"
        if status == 401:
            return ReasoningUnavailable("Anthropic rejected the API key — check it in settings")
        if status == 429:
            return ReasoningUnavailable("Anthropic rate limit reached — try again shortly")
        if status in (500, 502, 503, 529):
            return ReasoningUnavailable("Claude is unavailable right now")
        if "connect" in text.lower() or "timeout" in text.lower():
            return ReasoningUnavailable("could not reach Anthropic — check the network connection")
        return ReasoningUnavailable(f"advanced reasoning failed: {text[:200]}")

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # best-effort close
                await self._client.close()
            self._client = None


# --------------------------------------------------------------- translators


def _content_as_text(content: Any) -> str:
    """Flatten an OpenAI-style content value to plain text.

    Handles the vision case where content is a list of ``{"type":"text",...}`` /
    ``{"type":"image_url",...}`` parts — only the text parts survive, since a
    tool-result string is all Claude needs here.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "".join(parts)
    return "" if content is None else str(content)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert OpenAI chat messages to (system_text, Anthropic messages).

    * ``system`` messages are lifted into the returned system string.
    * ``tool`` results become ``tool_result`` blocks inside a user turn, merged
      with any immediately preceding tool results so all results for one
      assistant turn share a single user message (what Claude expects).
    * ``assistant`` messages with tool calls become ``tool_use`` blocks.
    * A leading ``assistant`` turn is dropped — Claude requires the first
      message to be from the user.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            text = _content_as_text(content)
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": _content_as_text(content),
            }
            if (
                out
                and out[-1]["role"] == "user"
                and isinstance(out[-1]["content"], list)
                and _is_tool_result_list(out[-1]["content"])
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = content if isinstance(content, str) else _content_as_text(content)
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": _parse_arguments(fn.get("arguments")),
                    }
                )
            if not blocks:
                continue  # Claude rejects an assistant turn with empty content
            if len(blocks) == 1 and blocks[0]["type"] == "text":
                out.append({"role": "assistant", "content": blocks[0]["text"]})
            else:
                out.append({"role": "assistant", "content": blocks})
            continue

        if role == "user":
            out.append({"role": "user", "content": _content_as_text(content)})

    # Claude requires the conversation to start with a user turn.
    while out and out[0]["role"] == "assistant":
        out.pop(0)

    return "\n\n".join(system_parts), out


def _is_tool_result_list(content: list[Any]) -> bool:
    return bool(content) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI function tool to an Anthropic tool.

    OpenAI: ``{"type":"function","function":{"name","description","parameters"}}``
    Anthropic: ``{"name","description","input_schema"}``
    """
    fn = tool.get("function", tool)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }
