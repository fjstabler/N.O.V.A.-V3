"""OpenAI client wrapper.

The only component in the project that talks to the internet. It is deliberately
thin: retries, timeouts, streaming and a typed result — no prompt logic, which
lives in :mod:`nova.ai.prompt`, and no tool dispatch, which lives in the skill
registry.

Streaming matters more here than it looks. The orchestrator forwards sentences
to text-to-speech as they arrive, so N.O.V.A. starts talking roughly a second
after you stop rather than after the whole reply is generated.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..runtime.errors import MissingDependency, NovaError
from ..runtime.logging import get_logger

log = get_logger(__name__)

#: Errors worth retrying; anything else (401, 400) will fail again identically.
_RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass(slots=True)
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ReasoningUnavailable(NovaError):
    code = "nova.reasoning.unavailable"


class OpenAIClient:
    """Async OpenAI chat client with retry and streaming."""

    def __init__(self, *, api_key: str, base_url: str = "", timeout: float = 60.0) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._client: Any = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def reconfigure(self, *, api_key: str, base_url: str = "", timeout: float = 60.0) -> None:
        """Swap credentials without restarting the process."""
        if (api_key, base_url, timeout) == (self._api_key, self._base_url, self._timeout):
            return
        self._api_key, self._base_url, self._timeout = api_key, base_url, timeout
        self._client = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ReasoningUnavailable(
                "no OpenAI API key configured — add one in settings to enable reasoning"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise MissingDependency("reasoning", "openai", "ai") from exc

        kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": self._timeout}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncOpenAI(**kwargs)
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
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        response = await self._with_retry(lambda: client.chat.completions.create(**request))
        choice = response.choices[0]
        message = choice.message
        completion = Completion(
            text=(message.content or "").strip(),
            finish_reason=choice.finish_reason or "",
        )
        for call in message.tool_calls or []:
            completion.tool_calls.append(
                ToolCall(
                    id=call.id, name=call.function.name, arguments=call.function.arguments or "{}"
                )
            )
        if getattr(response, "usage", None):
            completion.prompt_tokens = response.usage.prompt_tokens or 0
            completion.completion_tokens = response.usage.completion_tokens or 0
        return completion

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

        Tool calls arrive in fragments across deltas and are reassembled by index
        before the final completion is emitted.
        """
        client = self._ensure_client()
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        stream = await self._with_retry(lambda: client.chat.completions.create(**request))
        completion = Completion()
        partial_calls: dict[int, dict[str, str]] = {}

        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if getattr(delta, "content", None):
                completion.text += delta.content
                yield delta.content, None

            for call in getattr(delta, "tool_calls", None) or []:
                slot = partial_calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                if call.id:
                    slot["id"] = call.id
                if call.function and call.function.name:
                    slot["name"] = call.function.name
                if call.function and call.function.arguments:
                    slot["arguments"] += call.function.arguments

            if choice.finish_reason:
                completion.finish_reason = choice.finish_reason

        completion.text = completion.text.strip()
        for _, slot in sorted(partial_calls.items()):
            if slot["name"]:
                completion.tool_calls.append(
                    ToolCall(id=slot["id"], name=slot["name"], arguments=slot["arguments"] or "{}")
                )
        yield "", completion

    async def describe_image(
        self, prompt: str, image_data_url: str, *, model: str, max_tokens: int = 512
    ) -> str:
        client = self._ensure_client()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url, "detail": "auto"}},
                ],
            }
        ]
        response = await self._with_retry(
            lambda: client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens
            )
        )
        return (response.choices[0].message.content or "").strip()

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
                # Exponential backoff with jitter, so a rate limit does not
                # produce a thundering retry on the next turn either.
                delay = (2**attempt) + random.uniform(0, 0.5)
                log.warning(
                    "openai_retry", attempt=attempt + 1, delay=round(delay, 2), error=str(exc)[:200]
                )
                await asyncio.sleep(delay)
        raise self._normalise(last, getattr(last, "status_code", None)) from last

    def _normalise(self, exc: Exception | None, status: Any) -> NovaError:
        text = str(exc) if exc else "unknown error"
        if status == 401:
            return ReasoningUnavailable("OpenAI rejected the API key — check it in settings")
        if status == 429:
            return ReasoningUnavailable("OpenAI rate limit reached — try again shortly")
        if status in (500, 502, 503, 504):
            return ReasoningUnavailable("OpenAI is unavailable right now")
        if "connect" in text.lower() or "timeout" in text.lower():
            return ReasoningUnavailable("could not reach OpenAI — check the network connection")
        return ReasoningUnavailable(f"reasoning failed: {text[:200]}")

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # best-effort close
                await self._client.close()
            self._client = None
