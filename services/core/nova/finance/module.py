"""The finance module's one entry point.

Everything the assistant can ask about money goes through here, and everything
that comes back out is a finished sentence. That is the whole design: the
caller gets a string it can speak, not figures it might be tempted to hand to a
model for phrasing.

Nothing in this file writes to the bank except `payday_split`, which refuses
unless transfers are explicitly enabled, refuses again above a configured cap,
and defaults to logging what it would have done.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..runtime.errors import SkillError
from ..runtime.logging import get_logger
from . import phrasing, secrets
from .adapters.base import Balance, BankAdapter
from .adapters.csv_import import CsvAdapter
from .adapters.starling import StarlingAdapter
from .budget import Affordability, Outgoing, assess, recommend
from .ledger import Ledger, Transaction

log = get_logger(__name__)

#: What counts as a necessity when the model says so in its own words.
_NEEDS = frozenset({"need", "needs", "necessity", "necessary", "essential", "essentials"})


class FinanceModule:
    """Ledger, bank adapter and arithmetic, behind sentences."""

    def __init__(self, settings: Any, data_dir: Path) -> None:
        self._settings = settings
        self._data_dir = data_dir
        self.ledger = Ledger(data_dir / "finance.db")
        self._adapter: BankAdapter | None = None
        self._secrets = secrets.load(data_dir)

    async def open(self) -> None:
        await self.ledger.open()
        if not (self._data_dir / f"{secrets.FILENAME}.example").exists():
            secrets.write_example(self._data_dir)

    def reconfigure(self, settings: Any) -> None:
        """Settings changed; drop the adapter so the next call rebuilds it."""
        self._settings = settings
        self._adapter = None
        self._secrets = secrets.load(self._data_dir)

    # ---------------------------------------------------------------- the bank

    @property
    def adapter(self) -> BankAdapter:
        if self._adapter is None:
            self._adapter = self._build_adapter()
        return self._adapter

    def _build_adapter(self) -> BankAdapter:
        finance = self._settings
        if finance.provider == "starling":
            token = self._secrets.get("NOVA_FINANCE_TOKEN")
            if not token:
                raise SkillError(
                    "no bank token. Put NOVA_FINANCE_TOKEN in "
                    f"{self._data_dir / secrets.FILENAME} and chmod 600 it."
                )
            return StarlingAdapter(
                token, sandbox=finance.sandbox, account_name=finance.account_name
            )

        if not finance.statement_path:
            raise SkillError(
                "no statement file configured. Set finance.statement_path to a CSV "
                "export, or switch the provider to your bank."
            )
        return CsvAdapter(Path(finance.statement_path).expanduser())

    async def balance(self) -> Balance:
        return await self.adapter.balance()

    def _outgoings(self) -> list[Outgoing]:
        return [
            Outgoing(name=o.name, amount=o.amount, day_of_month=o.day_of_month)
            for o in self._settings.committed
        ]

    async def _assess(self, spend: float, today: date | None = None) -> Affordability:
        balance = await self.balance()
        return assess(
            balance=balance.effective,
            outgoings=self._outgoings(),
            today=today or datetime.now(UTC).date(),
            payday_day=self._settings.payday_day,
            spend=spend,
            rule=self._settings.payday_moves,
        )

    # -------------------------------------------------------------- questions

    async def affordability(self, spend: float = 0.0) -> str:
        """ "What have I got" and "can I afford £200" — the same figures either way.

        With advice on, a named spend also gets the conclusion: "can I afford
        £20" is not really a question about whether the card will work, it is
        a question about whether £20 leaves enough room between now and payday.
        The same thresholds as `advise`, so the two can never disagree.
        """
        result = await self._assess(max(0.0, spend))
        said = phrasing.affordability(result)
        if spend <= 0 or not self._settings.advice:
            return said
        # No need/want judgement here — nobody said what the £20 was for, and
        # "can I afford it" is a discretionary framing by default.
        verdict = recommend(result, kind="want", floor=self._settings.daily_floor)
        return f"{said} {phrasing.comfort(verdict, result)}"

    async def committed(self) -> str:
        """Why the available figure is lower than the balance."""
        return phrasing.committed_detail(await self._assess(0.0))

    async def advise(self, item: str, amount: float, kind: str) -> str:
        """ "Should I buy this?" — answered, rather than deflected with figures.

        `kind` is the model's read on whether the thing is a need or a want,
        which is a judgement about the item and not about the money: making it
        needs no access to the account, which is exactly why it can be the
        model's half of this. Everything numeric, and the conclusion drawn from
        it, happens here.
        """
        if not item.strip():
            raise SkillError("buy what?")
        if amount <= 0:
            raise SkillError("how much is it?")

        result = await self._assess(amount)
        if not self._settings.advice:
            # Turned off: back to reporting, which is what the brief asked for
            # and what anyone who did not ask for opinions should get.
            return phrasing.affordability(result)

        # Anything the model does not call a need is treated as a want. The
        # failure that matters is the other way round — a want waved through as
        # essential — so the default leans towards asking them to think.
        settled = "need" if kind.strip().lower() in _NEEDS else "want"
        verdict = recommend(result, kind=settled, floor=self._settings.daily_floor)
        return phrasing.advice(item, result, kind=settled, verdict=verdict)

    # ------------------------------------------------------------ cooling off

    async def want(self, item: str, amount: float) -> str:
        if not item.strip():
            raise SkillError("what is it you want to buy?")
        if amount <= 0:
            raise SkillError("how much is it?")
        hours = self._settings.cooling_off_hours
        ask_after = datetime.now(UTC) + timedelta(hours=hours)
        await self.ledger.add_pending(item.strip(), round(amount, 2), ask_after)
        when = "in an hour" if hours <= 1 else f"in {int(hours)} hours"
        return f"Noted: {item.strip()} for {phrasing.money(amount)}. I'll ask {when}."

    async def queue(self) -> str:
        pending = await self.ledger.pending()
        return phrasing.queue_summary([(p.item, p.amount) for p in pending])

    async def decide(self, item: str, outcome: str) -> str:
        """Record what happened to a waiting purchase."""
        choice = outcome.strip().lower()
        if choice in {"still thinking", "thinking", "later", "wait"}:
            return await self._requeue(item)
        if choice not in {"bought", "dropped"}:
            raise SkillError("say bought, dropped, or still thinking")

        match = await self._find_pending(item)
        await self.ledger.decide(match.id, choice)
        if choice == "dropped":
            return f"Dropped {match.item}. That's {phrasing.money(match.amount)} not spent."
        return f"Noted, you bought {match.item}."

    async def _requeue(self, item: str) -> str:
        match = await self._find_pending(item)
        hours = self._settings.cooling_off_hours
        await self.ledger.requeue(match.id, datetime.now(UTC) + timedelta(hours=hours))
        return f"Still thinking about {match.item}. I'll ask again in {int(hours)} hours."

    async def _find_pending(self, item: str) -> Any:
        pending = await self.ledger.pending()
        if not pending:
            raise SkillError("nothing is waiting")
        wanted = item.strip().lower()
        if not wanted:
            # One waiting item and no name is unambiguous; more than one is not.
            if len(pending) == 1:
                return pending[0]
            raise SkillError("which one? " + ", ".join(p.item for p in pending))
        for purchase in pending:
            if wanted in purchase.item.lower() or purchase.item.lower() in wanted:
                return purchase
        raise SkillError(f"nothing waiting matches {item!r}")

    async def dropped(self, days: int = 30) -> str:
        """The number the brief calls the module's most useful output."""
        since = datetime.now(UTC) - timedelta(days=days)
        count, total = await self.ledger.dropped_since(since)
        label = "in the last 30 days" if days == 30 else f"in the last {days} days"
        return phrasing.dropped_summary(count, total, label)

    async def prompt_for(self, purchase: Any) -> str:
        """The cooling-off question, with what buying it would leave.

        Deliberately does not mark the purchase as asked. Whether the question
        actually reached anyone is the caller's to know — do-not-disturb, quiet
        hours and a busy assistant can all swallow it — and marking it here
        would retire the one prompt the whole feature exists to deliver.
        """
        result = await self._assess(purchase.amount)
        return phrasing.cooling_off_prompt(
            purchase.item, purchase.amount, result.remaining_after_spend
        )

    # --------------------------------------------------------------- ingestion

    async def import_statement(self, path: str = "") -> str:
        chosen = path or self._settings.statement_path

        def read() -> tuple[CsvAdapter, int]:
            # Off the event loop: a statement can be a year of rows, and the
            # parse is pure CPU on top of the file read.
            adapter = CsvAdapter(Path(chosen).expanduser())
            return adapter, adapter.load()

        adapter, found = await asyncio.to_thread(read)
        stored = await self.ledger.record_transactions(
            await adapter.transactions_since(datetime(1970, 1, 1, tzinfo=UTC))
        )
        already = found - stored
        if already:
            return f"Read {found} transactions, {stored} new, {already} already known."
        return f"Read {found} transactions, all new."

    async def poll(self, days: int = 7) -> list[Transaction]:
        """Pull recent transactions from the bank, store them, return them.

        Returns what was fetched rather than what was new, because "new to the
        ledger" and "worth mentioning" are different questions: a statement
        imported for the first time is entirely new and none of it is news.
        The caller decides.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        fetched = await self.adapter.transactions_since(since)
        await self.ledger.record_transactions(fetched)
        return fetched

    async def refresh(self, days: int = 30) -> int:
        """Pull recent transactions from the bank into the ledger."""
        return len(await self.poll(days))

    # ---------------------------------------------------------------- alerting

    def is_large_spend(self, transaction: Transaction) -> bool:
        return (
            transaction.is_debit and abs(transaction.amount) >= self._settings.large_spend_threshold
        )

    def looks_like_salary(self, transaction: Transaction) -> bool:
        if transaction.is_debit or transaction.amount < self._settings.salary_min:
            return False
        pattern = self._settings.salary_pattern.strip().lower()
        if not pattern:
            return True
        return pattern in transaction.merchant.lower()

    # --------------------------------------------------------------- transfers

    async def payday_split(self, *, triggered_by: str = "manual") -> str:
        """Move the configured amount into savings.

        Four separate refusals stand between this and the account, because the
        cost of a bug here is somebody's money: transfers off by default, a dry
        run on by default under that, a hard cap under that, and an adapter
        that has to have opted into being able to move anything at all.
        """
        finance = self._settings
        amount = round(finance.transfer_amount, 2)

        if amount <= 0:
            raise SkillError("no transfer amount is configured")
        if not finance.transfer_pot:
            raise SkillError("no destination pot is configured")
        if amount > finance.transfer_max:
            raise SkillError(
                f"{phrasing.money(amount)} is over the {phrasing.money(finance.transfer_max)} "
                "cap, so nothing was moved. Raise finance.transfer_max if that is deliberate."
            )

        dry_run = finance.transfer_dry_run or not finance.enable_transfers
        if dry_run:
            await self.ledger.record_transfer(
                amount, finance.transfer_pot, dry_run=True, trigger=triggered_by
            )
            reason = (
                "transfers are not enabled" if not finance.enable_transfers else "dry run is on"
            )
            log.info("finance_transfer_dry_run", amount=amount, reason=reason)
            said = phrasing.transfer_done(amount, finance.transfer_pot, dry_run=True)
            return f"{said} ({reason}.)"

        mover = self.adapter
        if not hasattr(mover, "move_to_pot"):
            raise SkillError(f"the {mover.name} adapter cannot move money")

        await mover.move_to_pot(finance.transfer_pot, amount)
        await self.ledger.record_transfer(
            amount, finance.transfer_pot, dry_run=False, trigger=triggered_by
        )
        log.info("finance_transfer", amount=amount, destination=finance.transfer_pot)
        return phrasing.transfer_done(amount, finance.transfer_pot, dry_run=False)
