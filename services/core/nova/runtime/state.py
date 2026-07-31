"""The assistant state machine.

The Core animation is a direct function of this state, so the transitions have
to be strict — an illegal transition would show up on screen as a visual glitch.
Transient states (``ERROR``, ``NOTIFYING``) auto-return to the state they
interrupted, which is what makes a notification feel like an overlay rather than
a mode change.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum

from .events import EventBus, Topics
from .logging import get_logger

log = get_logger(__name__)


class NovaState(StrEnum):
    BOOTING = "booting"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"
    NOTIFYING = "notifying"


#: Legal transitions. Everything settles back to IDLE.
_TRANSITIONS: dict[NovaState, frozenset[NovaState]] = {
    NovaState.BOOTING: frozenset({NovaState.IDLE, NovaState.ERROR}),
    NovaState.IDLE: frozenset(
        {
            NovaState.LISTENING,
            NovaState.THINKING,
            NovaState.SPEAKING,
            NovaState.NOTIFYING,
            NovaState.ERROR,
        }
    ),
    NovaState.LISTENING: frozenset({NovaState.THINKING, NovaState.IDLE, NovaState.ERROR}),
    NovaState.THINKING: frozenset(
        {NovaState.SPEAKING, NovaState.IDLE, NovaState.LISTENING, NovaState.ERROR}
    ),
    NovaState.SPEAKING: frozenset(
        {NovaState.IDLE, NovaState.LISTENING, NovaState.THINKING, NovaState.ERROR}
    ),
    NovaState.ERROR: frozenset({NovaState.IDLE, NovaState.LISTENING}),
    NovaState.NOTIFYING: frozenset(
        {NovaState.IDLE, NovaState.LISTENING, NovaState.THINKING, NovaState.ERROR}
    ),
}

#: States that revert automatically after a timeout, and how long they last.
_TRANSIENT: dict[NovaState, float] = {
    NovaState.ERROR: 4.0,
    NovaState.NOTIFYING: 2.5,
}


class StateMachine:
    """Owns the single source of truth for what N.O.V.A. is doing."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._state = NovaState.BOOTING
        self._previous = NovaState.BOOTING
        self._lock = asyncio.Lock()
        self._revert_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> NovaState:
        return self._state

    @property
    def previous(self) -> NovaState:
        return self._previous

    def can_transition(self, target: NovaState) -> bool:
        return target is self._state or target in _TRANSITIONS[self._state]

    async def transition(self, target: NovaState, *, reason: str = "") -> bool:
        """Move to ``target``. Returns False if the transition is not legal."""
        async with self._lock:
            if target is self._state:
                return True
            if target not in _TRANSITIONS[self._state]:
                log.warning(
                    "illegal_state_transition", current=self._state, target=target, reason=reason
                )
                return False

            self._cancel_revert()
            self._previous = self._state
            self._state = target
            log.info("state", to=target.value, was=self._previous.value, reason=reason)
            self._bus.publish(
                Topics.STATE_CHANGED,
                {"state": target.value, "previous": self._previous.value, "reason": reason},
                source="state",
            )
            if target in _TRANSIENT:
                self._revert_task = asyncio.create_task(
                    self._revert_after(_TRANSIENT[target], target)
                )
            return True

    async def _revert_after(self, delay: float, from_state: NovaState) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._state is from_state:
            await self.transition(NovaState.IDLE, reason="transient-expired")

    def _cancel_revert(self) -> None:
        if self._revert_task is not None and not self._revert_task.done():
            self._revert_task.cancel()
        self._revert_task = None

    async def close(self) -> None:
        self._cancel_revert()
        if self._revert_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._revert_task
