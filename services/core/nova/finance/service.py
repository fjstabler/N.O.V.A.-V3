"""The finance module's lifecycle and the three things it does unprompted.

The skill answers questions when asked. This answers the ones nobody asked:

* a large debit lands and N.O.V.A. says so, from a webhook if the bank pushes
  one and from a poll otherwise;
* a purchase finishes cooling off and gets asked about, once;
* salary arrives and the payday split runs — as a dry run, until somebody
  turns both switches off.

The same rule applies here as everywhere else in this module: every sentence
below is built by `phrasing`, from numbers, on this machine. Nothing on these
paths reaches a model, which is why the alert is a notification with the words
already in it rather than a prompt asking one to comment on a purchase.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ..context import NovaContext
from ..notifications import Level, Notification, NotificationService
from ..runtime import Service
from ..runtime.errors import DegradedCapability, SkillError
from . import phrasing, secrets
from .ledger import PendingPurchase, Transaction
from .module import FinanceModule
from .webhook import WebhookEvent, WebhookReceiver

#: How often the cooling-off queue is checked. Cheap — one indexed read.
QUEUE_INTERVAL = 60.0

#: How long to wait before re-offering a prompt that could not be delivered
#: (do-not-disturb, quiet hours, or the assistant mid-conversation).
PROMPT_RETRY = timedelta(minutes=15)

#: How far back a poll looks. Wide enough to cover a missed run and a bank
#: backdating a card transaction when it settles.
POLL_WINDOW_DAYS = 7


class FinanceService(Service):
    """Owns the ledger, the bank connection and the background watching."""

    name = "finance"

    def __init__(self, ctx: NovaContext) -> None:
        super().__init__(ctx)
        self.module: FinanceModule | None = None
        self._receiver: WebhookReceiver | None = None
        #: Anything older than this was already true when the service started,
        #: so it is history rather than news. Without it, the first poll after
        #: an upgrade announces every large purchase of the past week.
        self._since = datetime.now(UTC)

    # -------------------------------------------------------------- lifecycle

    async def on_start(self) -> None:
        settings = self.ctx.settings.finance
        if not settings.enabled:
            raise DegradedCapability("finance", "disabled in settings")

        self._since = datetime.now(UTC)
        self.module = FinanceModule(settings, self.ctx.paths.data_dir)
        await self.module.open()

        await self._start_webhook()
        self.spawn(self._queue_loop(), name="finance-queue")
        if settings.refresh_minutes and settings.provider != "csv":
            self.spawn(self._poll_loop(), name="finance-poll")

    async def on_stop(self) -> None:
        if self._receiver is not None:
            await self._receiver.stop()
            self._receiver = None
        self.module = None

    def describe(self) -> str:
        settings = self.ctx.settings.finance
        parts: list[str] = [settings.provider]
        if self._receiver is not None and self._receiver.running:
            parts.append(f"webhook on {self._receiver.port}")
        if settings.refresh_minutes and settings.provider != "csv":
            parts.append(f"polling every {settings.refresh_minutes}m")
        if settings.enable_transfers and not settings.transfer_dry_run:
            parts.append("transfers live")
        return " · ".join(parts)

    # ---------------------------------------------------------------- webhook

    async def _start_webhook(self) -> None:
        settings = self.ctx.settings.finance
        if not settings.webhook_enabled:
            return

        store = secrets.load(self.ctx.paths.data_dir)
        secret = store.get("NOVA_FINANCE_WEBHOOK_SECRET")
        if not secret:
            # Refused rather than run unsigned. An open endpoint that accepts
            # anything claiming to be a transaction is worse than no endpoint:
            # it would let anyone on the network make N.O.V.A. announce a
            # purchase that never happened.
            self.log.warning(
                "finance_webhook_not_started",
                reason="no NOVA_FINANCE_WEBHOOK_SECRET",
                remedy=f"add it to {self.ctx.paths.data_dir / secrets.FILENAME}",
            )
            return

        self._receiver = WebhookReceiver(
            host=settings.webhook_host,
            port=settings.webhook_port,
            path=settings.webhook_path,
            secret=secret,
            scheme=settings.webhook_signature,
            handler=self._on_webhook,
        )
        try:
            await self._receiver.start()
        except OSError as exc:
            self._receiver = None
            self.log.warning("finance_webhook_bind_failed", error=str(exc))

    async def _on_webhook(self, event: WebhookEvent) -> None:
        if event.transaction is None:
            self.log.debug("finance_webhook_ignored", kind=event.kind)
            return
        if self.module is not None:
            await self.module.ledger.record_transactions([event.transaction])
        await self._consider(event.transaction, event_id=event.event_id)

    # ------------------------------------------------------------------ polls

    async def _poll_loop(self) -> None:
        while True:
            minutes = self.ctx.settings.finance.refresh_minutes
            if minutes <= 0:
                return
            await asyncio.sleep(minutes * 60)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except SkillError as exc:
                # A missing token or an unreachable bank is a condition to
                # report once a cycle, not a crash — the queue loop and the
                # question-answering side both still work.
                self.log.warning("finance_poll_failed", error=str(exc.message))
            except Exception:
                self.log.exception("finance_poll_failed")

    async def _poll_once(self) -> None:
        if self.module is None:
            return
        for transaction in await self.module.poll(POLL_WINDOW_DAYS):
            await self._consider(transaction, event_id=transaction.id)

    # --------------------------------------------------------------- alerting

    async def _consider(self, transaction: Transaction, *, event_id: str) -> None:
        """Decide whether one transaction is worth saying out loud.

        Two guards, and both are needed. The timestamp keeps history quiet: a
        first poll sees a week of transactions and none of them are news. The
        claim keeps a repeat quiet: the bank retries a delivery until it is
        acknowledged and the poll sees the same purchase every cycle, so the
        same transaction arrives many times as a matter of course.
        """
        module = self.module
        if module is None or transaction.happened_at < self._since:
            return
        if not await module.ledger.claim_event(f"seen:{event_id}"):
            return

        if module.is_large_spend(transaction):
            await self._announce_spend(transaction)
        elif module.looks_like_salary(transaction):
            await self._on_salary(transaction)

    async def _announce_spend(self, transaction: Transaction) -> None:
        module = self.module
        if module is None:
            return
        try:
            balance = (await module.balance()).effective
        except SkillError as exc:
            self.log.warning("finance_alert_balance_failed", error=str(exc.message))
            return

        # Merchant, amount, what is left. The brief allows nothing else here,
        # and there is nothing else worth saying: a running commentary on
        # somebody's spending is not an alert, it is nagging.
        await self._notify(
            title="Card spend",
            body=phrasing.large_spend(transaction.merchant, transaction.amount, balance),
            level=Level.INFO,
        )

    async def _on_salary(self, transaction: Transaction) -> None:
        module = self.module
        if module is None:
            return
        # Once per day at most. A salary credit that is amended, or two credits
        # in one day, must not run the split twice.
        stamp = transaction.happened_at.astimezone(UTC).date().isoformat()
        if not await module.ledger.claim_event(f"payday:{stamp}"):
            return

        settings = self.ctx.settings.finance
        if settings.transfer_amount <= 0 or not settings.transfer_pot:
            return

        try:
            said = await module.payday_split(triggered_by="salary")
        except SkillError as exc:
            said = str(exc.message)
        self.log.info("finance_payday_split", trigger="salary")
        await self._notify(title="Payday", body=said, level=Level.INFO)

    # ------------------------------------------------------------ cooling off

    async def _queue_loop(self) -> None:
        while True:
            await asyncio.sleep(QUEUE_INTERVAL)
            try:
                await self._ask_what_is_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("finance_queue_failed")

    async def _ask_what_is_due(self) -> None:
        module = self.module
        if module is None:
            return
        for purchase in await module.ledger.due_to_ask():
            await self._ask_about(purchase)

    async def _ask_about(self, purchase: PendingPurchase) -> None:
        module = self.module
        if module is None:
            return
        try:
            question = await module.prompt_for(purchase)
        except SkillError as exc:
            self.log.warning("finance_prompt_failed", error=str(exc.message))
            await module.ledger.requeue(purchase.id, datetime.now(UTC) + PROMPT_RETRY)
            return

        asked = await self._notify(title="Still want it?", body=question, level=Level.INFO)
        if asked:
            await module.ledger.mark_asked(purchase.id)
        else:
            # Not delivered, so not asked. Pushing it out rather than retrying
            # every minute keeps a do-not-disturb evening from becoming a loop
            # that fetches a balance sixty times an hour.
            await module.ledger.requeue(purchase.id, datetime.now(UTC) + PROMPT_RETRY)

    # ---------------------------------------------------------------- speaking

    async def _notify(self, *, title: str, body: str, level: Level) -> bool:
        """Raise a spoken notification, reporting whether it was delivered.

        Routed through the notification service rather than published on the
        bus, because the answer matters: quiet hours and do-not-disturb are
        allowed to swallow a money question, and the caller has to know so it
        can ask again later instead of dropping it.
        """
        notifications = self.ctx.service("notifications", NotificationService)
        if notifications is None:
            return False
        return await notifications.raise_notification(
            Notification(
                title=title,
                body=body,
                level=level,
                icon="finance",
                source="finance",
                timeout=20.0,
                speak=True,
            )
        )
