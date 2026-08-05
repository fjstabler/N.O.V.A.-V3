"""The turn orchestrator — where a sentence becomes an action.

One turn: recall memory, build the prompt, stream a completion, run whatever
tools the model asks for, stream the reply to speech, persist the exchange.

Two details do most of the work for how the assistant *feels*:

*Sentence streaming.* Text is cut at sentence boundaries as it arrives and
handed straight to synthesis, so speech begins about a second after the user
stops talking instead of after the full reply is generated.

*Confirmation as conversation.* A destructive tool call does not error out — it
becomes a spoken question, and the user's next "yes" resolves it. The pending
action is held by the skill registry with a short TTL.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..context import NovaContext
from ..runtime import NovaState, Service, Topics
from ..runtime.errors import ConfirmationRequired, NovaError
from ..skills.registry import SkillRegistry
from .client import Completion, OpenAIClient, ReasoningUnavailable, ToolCall
from .prompt import build_messages, build_system_prompt, facts_for_prompt, summarise_for_memory

#: Splits on sentence-ending punctuation followed by whitespace.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$")

#: Short affirmations that resolve a pending confirmation without a round trip.
_AFFIRMATIVE = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yes please",
        "do it",
        "go ahead",
        "confirm",
        "confirmed",
        "affirmative",
        "sure",
        "ok",
        "okay",
        "proceed",
        "please do",
        "correct",
    }
)
_NEGATIVE = frozenset(
    {
        "no",
        "nope",
        "cancel",
        "stop",
        "don't",
        "dont",
        "abort",
        "never mind",
        "nevermind",
        "forget it",
    }
)


@dataclass(slots=True)
class TurnResult:
    text: str = ""
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    awaiting_confirmation: bool = False


class Orchestrator(Service):
    """Drives one conversational turn from transcript to spoken reply."""

    name = "orchestrator"
    requires = ("memory", "skills")

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.client = OpenAIClient(
            api_key=ctx.settings.openai.api_key,
            base_url=ctx.settings.openai.base_url,
            timeout=ctx.settings.openai.request_timeout,
        )
        self._turn_lock = asyncio.Lock()
        self._current: asyncio.Task[TurnResult] | None = None
        self._last_activity = 0.0
        #: Source of the turn currently holding `_turn_lock`. Safe as plain
        #: instance state rather than a parameter threaded through every
        #: method, because the lock guarantees only one turn runs at a time.
        self._current_source = "voice"

    async def on_start(self) -> None:
        self.bus.subscribe(Topics.TRANSCRIPT_FINAL, self._on_transcript)
        self.ctx.store.on_change(self._on_settings_changed)
        if not self.ctx.settings.openai.configured:
            self.log.warning("reasoning_not_configured")

    async def on_stop(self) -> None:
        self.cancel_current()
        await self.client.close()

    def describe(self) -> str:
        settings = self.ctx.settings.openai
        return f"{settings.model}" if settings.configured else "no API key"

    def _on_settings_changed(self, settings: Any, changed: dict[str, Any]) -> None:
        if any(k.startswith("openai.") for k in changed):
            self.client.reconfigure(
                api_key=settings.openai.api_key,
                base_url=settings.openai.base_url,
                timeout=settings.openai.request_timeout,
            )

    # ------------------------------------------------------------------ entry

    async def _on_transcript(self, event: Any) -> None:
        text = (event.payload.get("text") or "").strip()
        if text:
            await self.handle(text, source=event.payload.get("source", "voice"))

    async def handle(self, text: str, *, source: str = "voice") -> TurnResult:
        """Process one user utterance. Cancels any turn already in flight."""
        self.cancel_current()
        self._current = asyncio.create_task(self._run_turn(text, source))
        try:
            return await self._current
        except asyncio.CancelledError:
            return TurnResult(error="cancelled")
        finally:
            self._current = None

    def cancel_current(self) -> None:
        if self._current is not None and not self._current.done():
            self._current.cancel()
            self.log.info("turn_cancelled")

    @property
    def busy(self) -> bool:
        return self._current is not None and not self._current.done()

    @property
    def last_activity(self) -> float:
        return self._last_activity

    # ------------------------------------------------------------------- turn

    async def _run_turn(self, text: str, source: str) -> TurnResult:
        async with self._turn_lock:
            self._current_source = source
            started = time.perf_counter()
            self._last_activity = time.time()
            self.bus.publish(
                Topics.TURN_STARTED, {"text": text, "source": source}, source=self.name
            )
            await self.ctx.state.transition(NovaState.THINKING, reason="user-input")

            try:
                result = await self._resolve(text)
            except asyncio.CancelledError:
                await self.ctx.state.transition(NovaState.IDLE, reason="cancelled")
                raise
            except NovaError as exc:
                message = self._annotate(exc)
                result = TurnResult(error=message)
                await self._fail(message)
            except Exception as exc:
                self.log.exception("turn_failed")
                result = TurnResult(error=str(exc))
                await self._fail("Something went wrong while I was thinking about that.")

            result.duration_ms = int((time.perf_counter() - started) * 1000)
            self.bus.publish(
                Topics.TURN_COMPLETED,
                {
                    "text": result.text,
                    "tools": result.tools_used,
                    "ms": result.duration_ms,
                    "error": result.error,
                },
                source=self.name,
            )
            if not result.error and self.ctx.state.state is NovaState.THINKING:
                await self.ctx.state.transition(NovaState.IDLE, reason="turn-complete")
            return result

    async def _resolve(self, text: str) -> TurnResult:
        registry = self._registry()

        # A bare "yes" after a gated action means "run it" — no model call needed.
        pending = registry.latest_pending() if registry else None
        if pending is not None:
            normalised = text.strip().lower().rstrip(".!")
            if normalised in _AFFIRMATIVE:
                return await self._run_confirmed(pending[0])
            if normalised in _NEGATIVE:
                registry.cancel(pending[0])
                return await self._speak_result("Cancelled.")

        if not self.ctx.settings.openai.configured:
            raise ReasoningUnavailable(
                "I don't have an OpenAI key yet. Add one in settings and I'll be able to think."
            )

        memory = self.ctx.service("memory")
        memories = facts_for_prompt(await memory.recall(text)) if memory else []
        history = await self._history()

        system_prompt = build_system_prompt(self.ctx, memories)
        messages = build_messages(system_prompt, history, text)

        if memory is not None:
            await memory.record_turn("user", text)

        result = await self._reason(messages, registry)

        if memory is not None and result.text:
            await memory.record_turn("assistant", result.text, tools=result.tools_used)
            # Store the exchange itself at low importance; the model promotes
            # anything worth keeping via the explicit remember tool.
            await memory.remember(
                summarise_for_memory(text, result.text),
                subject="conversation",
                importance=0.2,
                source="turn",
                ttl_seconds=14 * 86400,
            )
        return result

    async def _reason(
        self, messages: list[dict[str, Any]], registry: SkillRegistry | None
    ) -> TurnResult:
        settings = self.ctx.settings.openai
        tools = registry.openai_tools() if registry else []
        used: list[str] = []

        for iteration in range(settings.max_tool_iterations):
            # Only the final round streams to speech; intermediate rounds are
            # tool selection and would otherwise narrate their own plumbing.
            completion = await self._call_model(messages, tools, stream=iteration > 0 or not tools)

            if not completion.wants_tools:
                if not completion.text:
                    return await self._speak_result("I didn't get a response to that.")
                if iteration == 0 and tools:
                    # First round was non-streaming; speak it now.
                    return await self._speak_result(completion.text, tools_used=used)
                return TurnResult(text=completion.text, tools_used=used)

            messages.append(_assistant_tool_message(completion))
            for call in completion.tool_calls:
                outcome = await self._execute_tool(call, registry)
                if isinstance(outcome, ConfirmationRequired):
                    return await self._ask_for_confirmation(outcome, used)
                used.append(call.name)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": outcome})

        self.log.warning("tool_iteration_limit", limit=settings.max_tool_iterations)
        return await self._speak_result(
            "That needed more steps than I allow in one go. Ask me again and I'll continue.",
            tools_used=used,
        )

    async def _call_model(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool
    ) -> Completion:
        settings = self.ctx.settings.openai
        # Marks the boundary between local work and the network call, so a hang
        # can be attributed to one side or the other from the log alone.
        self.log.info("reasoning", model=settings.model, tools=len(tools), streaming=stream)
        if not stream:
            return await self.client.complete(
                messages,
                model=settings.model,
                tools=tools or None,
                temperature=settings.temperature,
                max_tokens=settings.max_output_tokens,
            )

        speaker = _SentenceStreamer(self)
        completion = Completion()
        async for delta, final in self.client.stream(
            messages,
            model=settings.model,
            tools=tools or None,
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
        ):
            if final is not None:
                completion = final
                break
            if delta:
                await speaker.feed(delta)
        await speaker.finish(completion.wants_tools)
        return completion

    async def _execute_tool(self, call: ToolCall, registry: SkillRegistry | None) -> Any:
        if registry is None:
            return "Tools are unavailable right now."
        self.log.info("tool_invoked", tool=call.name)
        try:
            result = await registry.call(call.name, call.arguments)
        except ConfirmationRequired as exc:
            return exc
        except NovaError as exc:
            return f"Error: {exc.message}"
        except Exception as exc:
            self.log.exception("tool_failed", tool=call.name)
            return f"Error: {exc}"
        return _stringify(result)

    # ---------------------------------------------------------- confirmations

    async def _ask_for_confirmation(
        self, request: ConfirmationRequired, used: list[str]
    ) -> TurnResult:
        question = f"{request.summary}. Should I go ahead?"
        self.bus.publish(
            Topics.NOTIFICATION,
            {
                "level": "prompt",
                "title": "Confirmation needed",
                "body": request.summary,
                "token": request.token,
                "timeout": 30.0,
                "actions": [
                    {"id": "confirm", "label": "Confirm", "token": request.token},
                    {"id": "cancel", "label": "Cancel", "token": request.token},
                ],
            },
            source=self.name,
        )
        result = await self._speak_result(question, tools_used=used)
        result.awaiting_confirmation = True
        return result

    async def _run_confirmed(self, token: str) -> TurnResult:
        registry = self._registry()
        if registry is None:
            return await self._speak_result("I can't do that right now.")
        try:
            outcome = await registry.confirm(token)
        except NovaError as exc:
            return await self._speak_result(exc.message)
        self.bus.publish(Topics.NOTIFICATION_DISMISS, {"token": token}, source=self.name)
        return await self._speak_result(_stringify(outcome) or "Done.")

    async def confirm(self, token: str) -> TurnResult:
        """Confirmation arriving from the UI rather than by voice."""
        return await self._run_confirmed(token)

    # ---------------------------------------------------------------- speaking

    async def _speak_result(self, text: str, *, tools_used: list[str] | None = None) -> TurnResult:
        """Emit a complete reply that did not come from the streaming path."""
        self.bus.publish(Topics.TURN_TEXT, {"text": text, "final": True}, source=self.name)
        await self._synthesise(text)
        return TurnResult(text=text, tools_used=tools_used or [])

    async def _synthesise(self, text: str) -> None:
        # A mobile client speaks its own reply (the phone's own TTS voice) —
        # the box at home must not also announce a query nobody there asked,
        # possibly made by someone who is not even in the house.
        if self._current_source == "mobile":
            return
        voice = self.ctx.service("voice")
        if voice is not None and text.strip():
            await voice.speak(text)

    def _annotate(self, exc: NovaError) -> str:
        """Add the one detail the raw error cannot know.

        A rejected key is confusing when an environment variable is quietly
        outranking the settings panel: the panel shows a key, saving it appears
        to work, and the assistant keeps using something else.
        """
        if "api key" in exc.message.lower() and ("openai.api_key" in self.ctx.store.env_overrides):
            return (
                f"{exc.message} It is currently coming from the NOVA_OPENAI__API_KEY "
                "environment variable, which overrides the settings panel."
            )
        return exc.message

    async def _fail(self, message: str) -> None:
        self.bus.publish(Topics.TURN_FAILED, {"error": message}, source=self.name)
        await self.ctx.state.transition(NovaState.ERROR, reason="turn-failed")
        await self._synthesise(message)

    # ----------------------------------------------------------------- helpers

    def _registry(self) -> SkillRegistry | None:
        return self.ctx.service("skills", SkillRegistry)

    async def _history(self) -> list[tuple[str, str]]:
        memory = self.ctx.service("memory")
        if memory is None:
            return []
        turns = await memory.working_context()
        return [(t.role, t.content) for t in turns if t.role in ("user", "assistant")]


class _SentenceStreamer:
    """Buffers streamed text and releases it one sentence at a time."""

    #: Below this length a "sentence" is usually an abbreviation, not a boundary.
    MIN_SENTENCE = 12

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator
        self._buffer = ""
        self._spoke = False

    async def feed(self, delta: str) -> None:
        self._buffer += delta
        self._orchestrator.bus.publish(
            Topics.TURN_TEXT, {"text": delta, "final": False}, source="orchestrator"
        )
        if not self._orchestrator.ctx.settings.voice.tts.stream_sentences:
            return
        while True:
            sentence, remainder = _split_sentence(self._buffer)
            if sentence is None or len(sentence) < self.MIN_SENTENCE:
                break
            self._buffer = remainder
            await self._speak(sentence)

    async def finish(self, had_tool_calls: bool) -> None:
        remaining = self._buffer.strip()
        self._buffer = ""
        # Text emitted alongside a tool call is the model narrating its plan;
        # speaking it would mean announcing the action twice.
        if remaining and not had_tool_calls:
            await self._speak(remaining)
        if self._spoke:
            self._orchestrator.bus.publish(
                Topics.TURN_TEXT, {"text": "", "final": True}, source="orchestrator"
            )

    async def _speak(self, sentence: str) -> None:
        self._spoke = True
        await self._orchestrator._synthesise(sentence)


def _split_sentence(buffer: str) -> tuple[str | None, str]:
    match = _SENTENCE_BOUNDARY.search(buffer)
    if match is None:
        return None, buffer
    return buffer[: match.end()].strip(), buffer[match.end() :]


def _assistant_tool_message(completion: Completion) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": completion.text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in completion.tool_calls
        ],
    }


def _stringify(value: Any) -> str:
    """Render a tool result as text the model can read."""
    if value is None:
        return "Done."
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if hasattr(value, "as_payload"):
        value = value.as_payload()
    if isinstance(value, (list, tuple)):
        if not value:
            return "(none)"
        return "\n".join(f"- {_stringify(item)}" for item in value[:50])
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_stringify(v)}" for k, v in value.items())
    return str(value)
