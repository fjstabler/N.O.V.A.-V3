"""Personal finance advisor: live Starling Bank balance plus expiring
reminders about one-off upcoming costs.

Two things this deliberately is not: a way to move money (every call here is
a GET; there is no path from any tool to a payment), and a way to intercept a
purchase before it happens (no bank exposes that to a third party — card
authorisation happens between the merchant, the card network and the bank,
with no hook for "ask my AI first"). What it gives instead is what actually
works: the user asks NOVA before they buy something, the way they would ring
an advisor, and gets a straight answer built from real numbers — the live
balance, what's already committed (Starling's own standing orders plus
anything the user has told NOVA to expect), and recent spending.

Upcoming one-off costs ("the vet bill is £200, due Tuesday") are stored
through the existing memory service rather than a new table — they are just
memories of kind EVENT, subject 'upcoming-expense', with a TTL computed from
the due date plus a grace period, so a bill quietly stops being mentioned
once it has passed rather than nagging forever.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from ...integrations.calendar import parse_when
from ...memory.models import MemoryKind
from ...memory.service import MemoryService
from ...runtime.errors import SkillError
from ..base import Param, Skill, tool

#: Feed items in these states never happened (or were undone) and shouldn't
#: count as spending.
_EXCLUDED_FEED_STATUSES = {"DECLINED", "REVERSED", "FAILED"}


class FinanceSkill(Skill):
    name = "finance"
    description = "Live bank balance, spending and upcoming-cost tracking for judging a purchase."
    category = "Finance"
    prompt_hint = (
        "This is a personal finance advisor, not a payment system — it can never move money, "
        "freeze a card or hold up a purchase (no bank gives a third party that kind of control). "
        "Use it when asked to check the balance, weigh up whether a purchase is a good idea, or "
        "remember a one-off upcoming cost. When judging a purchase, weigh the live balance, "
        "anything already committed (standing orders and remembered upcoming costs) and recent "
        "spending against the price, then give a direct, honest opinion — don't just recite the "
        "numbers back and leave the judgement to the user. Never imply the purchase itself can be "
        "stopped or held here; the decision, and tapping the card, are always the user's."
    )

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.finance.enabled:
            return False, "finance skill disabled in settings"
        if self.ctx.service("memory") is None:
            return False, "memory service is not running"
        return True, ""

    @property
    def memory(self) -> MemoryService:
        return self.ctx.require("memory", MemoryService)

    def context_lines(self) -> list[str]:
        if self.ctx.settings.finance.starling_access_token.strip():
            return [
                "A Starling Bank account is connected — live balance and spending are available."
            ]
        return [
            "No bank account connected — only manually remembered upcoming costs are available, "
            "not a live balance. Point to Settings → Finance if a live balance would help."
        ]

    # ------------------------------------------------------------------ bank

    @tool("Get the live Starling Bank balance.")
    async def check_balance(self) -> str:
        account = await self._default_account()
        balance = await self._starling_get(f"/accounts/{account['accountUid']}/balance")
        cleared = _money(balance.get("clearedBalance"))
        effective = _money(balance.get("effectiveBalance"))
        line = f"Cleared balance: £{cleared:.2f}."
        if abs(effective - cleared) > 0.005:
            line += f" Effective balance once pending transactions settle: £{effective:.2f}."
        return line

    @tool(
        "Check whether a purchase is a responsible idea right now — weighs the live balance, "
        "what's already committed (standing orders, remembered upcoming costs) and recent "
        "spending against the price. Use for 'can I afford X', 'should I buy X', or when called "
        "to ask about a specific purchase."
    )
    async def check_affordability(
        self,
        item: Annotated[
            str, Param("What's being considered", examples=("a new phone", "a takeaway"))
        ],
        price: Annotated[float, Param("Price in pounds")],
    ) -> str:
        lines = [f"Considering: {item} — £{price:.2f}"]
        committed = 0.0
        effective: float | None = None
        token = self.ctx.settings.finance.starling_access_token.strip()

        if not token:
            lines.append(
                "No bank account connected, so this is based only on what's been remembered, "
                "not live numbers."
            )
        else:
            try:
                account = await self._default_account()
                balance = await self._starling_get(f"/accounts/{account['accountUid']}/balance")
                effective = _money(balance.get("effectiveBalance"))
                lines.append(
                    f"Live balance right now (after pending transactions): £{effective:.2f}"
                )

                days = self.ctx.settings.finance.recent_spend_days
                spent, count = await self._recent_spend(account, days)
                lines.append(
                    f"Spent in the last {days} days: £{spent:.2f} across {count} transaction(s) "
                    f"(£{spent / days:.2f}/day average)"
                )

                standing_total, standing_count = await self._standing_orders(account)
                if standing_total is not None:
                    committed += standing_total
                    lines.append(
                        f"Standing orders currently set up: £{standing_total:.2f} total across "
                        f"{standing_count} payment(s)"
                    )
            except SkillError as exc:
                lines.append(f"Couldn't reach the live Starling balance: {exc}")

        upcoming = await self._list_upcoming()
        if upcoming:
            committed += sum(one["amount"] for one in upcoming)
            lines.append("Upcoming costs already told about:")
            lines.extend(
                f"  - {one['description']}: £{one['amount']:.2f}, due {one['due_label']}"
                for one in upcoming
            )
        else:
            lines.append("No upcoming one-off costs remembered.")

        if effective is not None:
            remaining = effective - committed - price
            lines.append(
                f"After this purchase and everything already committed above: £{remaining:.2f} "
                "left."
            )

        lines.append(
            "Weigh this up and give a direct opinion — you cannot hold or block the purchase, "
            "only advise."
        )
        return "\n".join(lines)

    # -------------------------------------------------------------- upcoming

    @tool(
        "Remember a one-off upcoming cost so it's weighed into affordability checks — a bill, a "
        "booked trip, a renewal. Stops being mentioned a few days after it's due.",
        mutating=True,
    )
    async def add_upcoming_expense(
        self,
        description: Annotated[
            str,
            Param("What it's for", examples=("council tax", "vet bill", "car insurance renewal")),
        ],
        amount: Annotated[float, Param("Amount in pounds")],
        due: Annotated[
            str, Param("When it's due", examples=("next Tuesday", "the 3rd", "in 10 days"))
        ],
        notes: Annotated[str, Param("Anything else worth knowing")] = "",
    ) -> str:
        due_at, _ = parse_when(due)
        grace = timedelta(days=self.ctx.settings.finance.upcoming_expense_grace_days)
        ttl_seconds = max((due_at + grace - datetime.now()).total_seconds(), 3600.0)
        description = description.strip()
        await self.memory.remember(
            f"{description} — £{amount:.2f}, due {due_at:%A %d %B}",
            kind=MemoryKind.EVENT,
            subject="upcoming-expense",
            importance=0.6,
            source="explicit",
            metadata={
                "description": description,
                "amount": amount,
                "due_at": due_at.timestamp(),
                "notes": notes,
            },
            ttl_seconds=ttl_seconds,
        )
        return f"Noted — {description}, £{amount:.2f}, due {due_at:%A %d %B}."

    @tool("List upcoming one-off costs that have been remembered and haven't expired yet.")
    async def list_upcoming_expenses(self) -> str:
        upcoming = await self._list_upcoming()
        if not upcoming:
            return "Nothing remembered."
        return "\n".join(
            f"{one['description']}: £{one['amount']:.2f}, due {one['due_label']}"
            for one in upcoming
        )

    @tool(
        "Stop tracking a previously remembered upcoming cost — plans changed, or it's already "
        "been paid.",
        mutating=True,
    )
    async def cancel_upcoming_expense(
        self, description: Annotated[str, Param("Which one, by name")]
    ) -> str:
        needle = description.strip().lower()
        if not needle:
            raise SkillError("Which upcoming cost do you mean?")
        memories = await self.memory.list_memories(MemoryKind.EVENT, limit=200)
        now = time.time()
        for memory in memories:
            if memory.subject != "upcoming-expense":
                continue
            if memory.expires_at is not None and memory.expires_at < now:
                continue
            stored = str(memory.metadata.get("description", "")).lower()
            if needle in stored or stored in needle:
                if memory.id is not None:
                    await self.memory.forget(memory.id)
                return f"Removed '{memory.metadata.get('description', description)}'."
        raise SkillError(f"Nothing upcoming matching '{description}'.")

    async def _list_upcoming(self) -> list[dict[str, Any]]:
        memories = await self.memory.list_memories(MemoryKind.EVENT, limit=200)
        now = time.time()
        out: list[dict[str, Any]] = []
        for memory in memories:
            if memory.subject != "upcoming-expense":
                continue
            if memory.expires_at is not None and memory.expires_at < now:
                continue
            due_at = memory.metadata.get("due_at")
            due_label = (
                datetime.fromtimestamp(due_at).strftime("%A %d %B") if due_at else "an unknown date"
            )
            out.append(
                {
                    "description": memory.metadata.get("description", memory.content),
                    "amount": float(memory.metadata.get("amount", 0.0)),
                    "due_label": due_label,
                    "due_at": float(due_at or 0.0),
                }
            )
        out.sort(key=lambda one: one["due_at"])
        return out

    # ------------------------------------------------------------- starling

    def _starling_base_url(self) -> str:
        if self.ctx.settings.finance.starling_sandbox:
            return "https://api-sandbox.starlingbank.com/api/v2"
        return "https://api.starlingbank.com/api/v2"

    async def _starling_get(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        token = self.ctx.settings.finance.starling_access_token.strip()
        if not token:
            raise SkillError(
                "No Starling access token configured — add one under Settings → Finance."
            )
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as client:
                response = await client.get(f"{self._starling_base_url()}{path}", params=params)
        except httpx.HTTPError as exc:
            raise SkillError(f"Couldn't reach Starling: {exc}") from exc

        if response.status_code == 401:
            raise SkillError(
                "Starling rejected the access token — generate a fresh one from the developer "
                "portal."
            )
        if response.status_code == 403:
            raise SkillError(
                "The Starling access token doesn't have permission for this — check its scopes."
            )
        if response.status_code >= 400:
            raise SkillError(f"Starling returned {response.status_code} for {path}.")
        return response.json() or {}

    async def _default_account(self) -> dict[str, Any]:
        data = await self._starling_get("/accounts")
        accounts = data.get("accounts") or []
        if not accounts:
            raise SkillError("Starling returned no accounts for this token.")
        wanted = self.ctx.settings.finance.account_name.strip().lower()
        if wanted:
            for account in accounts:
                if str(account.get("name", "")).strip().lower() == wanted:
                    return account
        return accounts[0]

    async def _recent_spend(self, account: dict[str, Any], days: int) -> tuple[float, int]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        data = await self._starling_get(
            f"/feed/account/{account['accountUid']}/category/{account['defaultCategory']}",
            params={"changesSince": since},
        )
        total = 0.0
        count = 0
        for feed_item in data.get("feedItems") or []:
            if feed_item.get("direction") != "OUT":
                continue
            if feed_item.get("status") in _EXCLUDED_FEED_STATUSES:
                continue
            total += _money(feed_item.get("amount"))
            count += 1
        return total, count

    async def _standing_orders(self, account: dict[str, Any]) -> tuple[float | None, int]:
        """Best-effort. Starling's standing-order response shape is the one part of this
        integration that couldn't be pinned down from public documentation alone, so any
        surprise in it is swallowed here rather than breaking the whole affordability check."""
        try:
            data = await self._starling_get(
                f"/payments/local/account/{account['accountUid']}"
                f"/category/{account['defaultCategory']}/standing-orders"
            )
        except SkillError:
            return None, 0
        orders = data.get("standingOrders") or data.get("standingOrderPaymentOrders") or []
        total = 0.0
        count = 0
        for order in orders:
            if order.get("cancelledAt"):
                continue
            total += _money(order.get("amount"))
            count += 1
        return total, count


def _money(amount: dict[str, Any] | None) -> float:
    if not amount:
        return 0.0
    return float(amount.get("minorUnits", 0)) / 100
