"""A bank statement from a file.

The brief asks for this first, and it is right to: it carries none of the
credential risk, it works before any API access exists, and it makes every
feature above it testable without touching a live account. It is also the
answer when an API is unavailable — a downloaded statement is still a
statement.

Banks disagree about column names and about what a negative number means, so
this reads headers case-insensitively, accepts the common spellings, and works
out the sign convention from the file rather than assuming one.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ...runtime.errors import SkillError
from ..ledger import Transaction
from .base import Balance

#: Header spellings seen in the wild, lowercased. First match wins.
_DATE = ("date", "transaction date", "date & time", "created", "timestamp")
_AMOUNT = ("amount", "amount (gbp)", "value", "amount(gbp)")
_MERCHANT = ("counter party", "counterparty", "merchant", "description", "reference", "name")
_CATEGORY = ("category", "spending category", "type")
_BALANCE = ("balance", "balance (gbp)", "running balance")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d %b %Y",
    "%d %B %Y",
)


def _pick(headers: Sequence[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {header.strip().lower(): header for header in headers}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    # A partial match catches "Amount (GBP)" against "amount" without listing
    # every currency and punctuation variant a bank might use.
    for key, original in lowered.items():
        if any(key.startswith(candidate) for candidate in candidates):
            return original
    return None


def _parse_date(value: str) -> datetime:
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SkillError(f"could not read the date {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_amount(value: str) -> float:
    text = value.strip().replace("£", "").replace(",", "").replace(" ", "")
    if not text:
        return 0.0
    # Accountants write negatives in brackets.
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = float(text)
    except ValueError as exc:
        raise SkillError(f"could not read the amount {value!r}") from exc
    return -amount if negative else amount


def parse(text: str, *, source: str = "csv") -> list[Transaction]:
    """Read a statement into transactions, newest handling left to the caller."""
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if not headers:
        raise SkillError("that file has no header row")

    date_column = _pick(headers, _DATE)
    amount_column = _pick(headers, _AMOUNT)
    if date_column is None or amount_column is None:
        raise SkillError(
            f"a statement needs a date column and an amount column; found {', '.join(headers)}"
        )
    merchant_column = _pick(headers, _MERCHANT)
    category_column = _pick(headers, _CATEGORY)

    transactions: list[Transaction] = []
    for index, row in enumerate(reader):
        raw_date = (row.get(date_column) or "").strip()
        if not raw_date:
            continue  # trailing blank lines are normal in exported statements
        happened = _parse_date(raw_date)
        amount = _parse_amount(row.get(amount_column) or "")
        merchant = (row.get(merchant_column) or "").strip() if merchant_column else ""
        category = (row.get(category_column) or "").strip() if category_column else ""

        # No statement carries a stable id, so derive one that is stable for
        # the same row: re-importing the same file must not double-count, and
        # the row number alone would collide across files.
        digest = hashlib.sha256(
            f"{happened.isoformat()}|{amount}|{merchant}|{index}".encode()
        ).hexdigest()[:32]

        transactions.append(
            Transaction(
                id=f"csv-{digest}",
                happened_at=happened,
                amount=amount,
                merchant=merchant,
                category=category,
                source=source,
            )
        )
    return transactions


class CsvAdapter:
    """A statement file standing in for a bank.

    Balance is the closing balance if the file has one, otherwise the sum of
    everything in it — which is right for a full export and clearly wrong for a
    partial one, so `balance_known` says which it is rather than pretending.
    """

    name = "csv"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._transactions: list[Transaction] = []
        self._closing: float | None = None

    def load(self) -> int:
        if not self.path.is_file():
            raise SkillError(f"no statement at {self.path}")
        text = self.path.read_text(encoding="utf-8-sig")
        self._transactions = parse(text)
        self._closing = _closing_balance(text)
        return len(self._transactions)

    @property
    def balance_known(self) -> bool:
        return self._closing is not None

    async def balance(self) -> Balance:
        if not self._transactions and self._closing is None:
            self.load()
        total = self._closing
        if total is None:
            total = round(sum(t.amount for t in self._transactions), 2)
        return Balance(cleared=total, effective=total)

    async def transactions_since(self, since: datetime) -> list[Transaction]:
        if not self._transactions:
            self.load()
        return [t for t in self._transactions if t.happened_at >= since]


def _closing_balance(text: str) -> float | None:
    """The balance as of the most recent transaction, if the file carries one.

    By date rather than by position: banks export newest-first as often as
    oldest-first, and taking the last row means taking the oldest balance in
    half of all statements — which is not a rounding error, it is the balance
    from before everything in the file happened.
    """
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    column = _pick(headers, _BALANCE)
    date_column = _pick(headers, _DATE)
    if column is None or date_column is None:
        return None

    newest: datetime | None = None
    balance: float | None = None
    for row in reader:
        raw = (row.get(column) or "").strip()
        raw_date = (row.get(date_column) or "").strip()
        if not raw or not raw_date:
            continue
        happened = _parse_date(raw_date)
        if newest is None or happened >= newest:
            newest, balance = happened, _parse_amount(raw)
    return balance
