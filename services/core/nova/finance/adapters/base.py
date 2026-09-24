"""What the rest of the module is allowed to ask a bank for.

Deliberately small. Everything above this line works in balances and
transactions; nothing above it knows what a Starling account UID is or that
Monzo pays in a different shape. Adding Monzo means writing one file, not
finding every place that assumed Starling.

Read-only by construction: the only write in the interface is `move_to_pot`,
and it is a separate protocol that an adapter can decline to implement. An
adapter with no transfer support is not a broken adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..ledger import Transaction


@dataclass(frozen=True, slots=True)
class Balance:
    """What the account holds.

    `cleared` is settled money; `effective` includes authorisations that have
    not landed yet. Spending against `cleared` is how people end up surprised,
    so the module reports `effective` and this type keeps both so the
    difference is never lost by accident.
    """

    cleared: float
    effective: float
    currency: str = "GBP"


@runtime_checkable
class BankAdapter(Protocol):
    """Read-only access to one account."""

    name: str

    async def balance(self) -> Balance: ...

    async def transactions_since(self, since: datetime) -> list[Transaction]: ...


@runtime_checkable
class SupportsTransfers(Protocol):
    """Separate on purpose: an adapter opts in to being able to move money."""

    async def move_to_pot(self, pot: str, amount: float) -> str:
        """Move `amount` into a savings pot. Returns a reference for the log."""
        ...
