"""The finance module's own SQLite database.

Deliberately separate from `memory.db`. Conversation memory is summarised,
pruned on a TTL and fed to a model; none of those are things to do to a
transaction log. Keeping them apart also means the finance module can be
deleted — file and all — without touching anything else.

Four tables, one per feature that needs to remember something: transactions
seen, purchases waiting out their cooling-off period, transfers executed, and
the webhook deliveries already handled.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..runtime.logging import get_logger

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    happened_at   TEXT NOT NULL,
    amount        REAL NOT NULL,      -- negative for money out
    currency      TEXT NOT NULL DEFAULT 'GBP',
    merchant      TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL       -- 'csv' | 'starling' | 'webhook'
);
CREATE INDEX IF NOT EXISTS transactions_when ON transactions(happened_at);

CREATE TABLE IF NOT EXISTS cooling_off (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item          TEXT NOT NULL,
    amount        REAL NOT NULL,
    added_at      TEXT NOT NULL,
    ask_after     TEXT NOT NULL,
    asked_at      TEXT,
    outcome       TEXT,               -- NULL while pending | 'bought' | 'dropped'
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS cooling_off_pending ON cooling_off(outcome, ask_after);

CREATE TABLE IF NOT EXISTS transfers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at   TEXT NOT NULL,
    amount        REAL NOT NULL,
    destination   TEXT NOT NULL,
    dry_run       INTEGER NOT NULL,
    trigger       TEXT NOT NULL DEFAULT ''
);

-- Webhook deliveries already acted on. The bank retries until it gets a 2xx,
-- so the same transaction arrives more than once as a matter of course.
CREATE TABLE IF NOT EXISTS handled_events (
    event_id      TEXT PRIMARY KEY,
    handled_at    TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Transaction:
    id: str
    happened_at: datetime
    amount: float
    merchant: str
    currency: str = "GBP"
    category: str = ""
    source: str = "csv"

    @property
    def is_debit(self) -> bool:
        return self.amount < 0


@dataclass(frozen=True, slots=True)
class PendingPurchase:
    id: int
    item: str
    amount: float
    added_at: datetime
    ask_after: datetime
    asked_at: datetime | None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class Ledger:
    """Every write goes through a thread, so the event loop never blocks on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        # A finance database that survives a power cut half-written is worse
        # than one that is slightly slower.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    async def open(self) -> None:
        def setup() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(SCHEMA)
            # Balances and merchant names: nobody else's business.
            self.path.chmod(0o600)

        await asyncio.to_thread(setup)
        log.info("finance_ledger_ready", path=str(self.path))

    # ------------------------------------------------------------ transactions

    async def record_transactions(self, transactions: list[Transaction]) -> int:
        """Insert, ignoring any whose id is already known. Returns how many were new."""
        if not transactions:
            return 0

        def write() -> int:
            with self._connect() as connection:
                before = connection.total_changes
                connection.executemany(
                    "INSERT OR IGNORE INTO transactions"
                    " (id, happened_at, amount, currency, merchant, category, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            t.id,
                            _iso(t.happened_at),
                            t.amount,
                            t.currency,
                            t.merchant,
                            t.category,
                            t.source,
                        )
                        for t in transactions
                    ],
                )
                return connection.total_changes - before

        async with self._lock:
            return await asyncio.to_thread(write)

    async def spend_since(self, since: datetime) -> float:
        """Total spent (a positive number) since `since`."""

        def read() -> float:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions"
                    " WHERE amount < 0 AND happened_at >= ?",
                    (_iso(since),),
                ).fetchone()
                return abs(float(row["total"]))

        return await asyncio.to_thread(read)

    # ------------------------------------------------------------ cooling off

    async def add_pending(self, item: str, amount: float, ask_after: datetime) -> int:
        def write() -> int:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO cooling_off (item, amount, added_at, ask_after)"
                    " VALUES (?, ?, ?, ?)",
                    (item, amount, _iso(_now()), _iso(ask_after)),
                )
                return int(cursor.lastrowid or 0)

        async with self._lock:
            return await asyncio.to_thread(write)

    async def pending(self) -> list[PendingPurchase]:
        def read() -> list[PendingPurchase]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM cooling_off WHERE outcome IS NULL ORDER BY ask_after"
                ).fetchall()
                return [
                    PendingPurchase(
                        id=int(row["id"]),
                        item=row["item"],
                        amount=float(row["amount"]),
                        added_at=_parse(row["added_at"]),
                        ask_after=_parse(row["ask_after"]),
                        asked_at=_parse(row["asked_at"]) if row["asked_at"] else None,
                    )
                    for row in rows
                ]

        return await asyncio.to_thread(read)

    async def due_to_ask(self, now: datetime | None = None) -> list[PendingPurchase]:
        """Pending items whose delay has elapsed and which have not been asked about."""
        moment = now or _now()
        return [p for p in await self.pending() if p.asked_at is None and p.ask_after <= moment]

    async def mark_asked(self, purchase_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update, purchase_id, "asked_at", _iso(_now()))

    async def requeue(self, purchase_id: int, ask_after: datetime) -> None:
        """ "Still thinking" — push the question out and allow it to be asked again."""

        def write() -> None:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE cooling_off SET ask_after = ?, asked_at = NULL WHERE id = ?",
                    (_iso(ask_after), purchase_id),
                )

        async with self._lock:
            await asyncio.to_thread(write)

    async def decide(self, purchase_id: int, outcome: str) -> None:
        def write() -> None:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE cooling_off SET outcome = ?, decided_at = ? WHERE id = ?",
                    (outcome, _iso(_now()), purchase_id),
                )

        async with self._lock:
            await asyncio.to_thread(write)

    async def dropped_since(self, since: datetime) -> tuple[int, float]:
        """How many purchases were dropped, and what they would have cost."""

        def read() -> tuple[int, float]:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM cooling_off"
                    " WHERE outcome = 'dropped' AND decided_at >= ?",
                    (_iso(since),),
                ).fetchone()
                return int(row["n"]), float(row["total"])

        return await asyncio.to_thread(read)

    # --------------------------------------------------------------- transfers

    async def record_transfer(
        self, amount: float, destination: str, *, dry_run: bool, trigger: str = ""
    ) -> None:
        def write() -> None:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO transfers (executed_at, amount, destination, dry_run, trigger)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (_iso(_now()), amount, destination, 1 if dry_run else 0, trigger),
                )

        async with self._lock:
            await asyncio.to_thread(write)

    async def transfers(self, limit: int = 20) -> list[dict[str, object]]:
        def read() -> list[dict[str, object]]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM transfers ORDER BY executed_at DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(read)

    # ------------------------------------------------------------ webhook dedupe

    async def claim_event(self, event_id: str) -> bool:
        """Record an event id, returning True only the first time it is seen.

        The bank retries a delivery until it is acknowledged, so the same
        transaction arrives repeatedly as a matter of course. Claiming the id
        before acting is what keeps one purchase to one alert.
        """

        def write() -> bool:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO handled_events (event_id, handled_at) VALUES (?, ?)",
                    (event_id, _iso(_now())),
                )
                return cursor.rowcount > 0

        async with self._lock:
            return await asyncio.to_thread(write)

    # ---------------------------------------------------------------- internal

    def _update(self, purchase_id: int, column: str, value: str) -> None:
        # Column names are internal constants, never anything a caller supplies.
        with self._connect() as connection:
            connection.execute(
                f"UPDATE cooling_off SET {column} = ? WHERE id = ?",
                (value, purchase_id),
            )
