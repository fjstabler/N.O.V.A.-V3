"""Starling, read-only except for one deliberate exception.

Personal access tokens rather than OAuth: Starling issues them to account
holders directly, they do not expire on a schedule, and there is no refresh
dance to get wrong. Monzo would need the refresh handling the brief describes;
that belongs in a `monzo.py` next to this one, behind the same interface.

Every method here is a GET apart from `move_to_pot`, which is the single write
the brief permits and which nothing calls unless transfers are explicitly
enabled in config.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ...runtime.errors import SkillError
from ...runtime.logging import get_logger
from ..ledger import Transaction
from .base import Balance

log = get_logger(__name__)

LIVE = "https://api.starlingbank.com/api/v2"
SANDBOX = "https://api-sandbox.starlingbank.com/api/v2"


class StarlingAdapter:
    """One Starling account, reached with a personal access token."""

    name = "starling"

    def __init__(self, token: str, *, sandbox: bool = False, account_name: str = "") -> None:
        if not token:
            raise SkillError("no Starling token: put NOVA_FINANCE_TOKEN in finance.env")
        self._token = token
        self._base = SANDBOX if sandbox else LIVE
        self._account_name = account_name
        self._account: tuple[str, str] | None = None  # (accountUid, defaultCategory)

    # ------------------------------------------------------------------ wire

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path)

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a hard dependency
            raise SkillError("httpx is required to reach Starling") from exc

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.request(
                    method,
                    f"{self._base}{path}",
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/json",
                        "User-Agent": "nova-finance",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise SkillError(f"could not reach Starling: {exc}") from exc

        if response.status_code == 403:
            raise SkillError(
                "Starling refused the token. Check it has the scopes this needs "
                "(account:read, balance:read, transaction:read)."
            )
        if response.status_code >= 400:
            # Deliberately not echoing the body: it can carry account details,
            # and this message may end up in a log or on a screen.
            raise SkillError(f"Starling returned {response.status_code}")
        try:
            return dict(response.json())
        except ValueError as exc:
            raise SkillError("Starling sent something that was not JSON") from exc

    async def _resolve_account(self) -> tuple[str, str]:
        if self._account is not None:
            return self._account
        payload = await self._get("/accounts")
        accounts = payload.get("accounts") or []
        if not accounts:
            raise SkillError("that Starling token has no accounts on it")
        chosen = accounts[0]
        if self._account_name:
            wanted = self._account_name.strip().lower()
            for account in accounts:
                if wanted in str(account.get("name", "")).lower():
                    chosen = account
                    break
        self._account = (str(chosen["accountUid"]), str(chosen["defaultCategory"]))
        return self._account

    # ---------------------------------------------------------------- reading

    async def balance(self) -> Balance:
        account_uid, _ = await self._resolve_account()
        payload = await self._get(f"/accounts/{account_uid}/balance")
        return Balance(
            cleared=_amount(payload.get("clearedBalance")),
            effective=_amount(payload.get("effectiveBalance")),
            currency=str((payload.get("effectiveBalance") or {}).get("currency", "GBP")),
        )

    async def transactions_since(self, since: datetime) -> list[Transaction]:
        account_uid, category = await self._resolve_account()
        stamp = since.astimezone().isoformat()
        payload = await self._get(
            f"/feed/account/{account_uid}/category/{category}?changesSince={stamp}"
        )
        return [_transaction(item) for item in payload.get("feedItems") or []]

    # ---------------------------------------------------------------- writing

    async def move_to_pot(self, pot: str, amount: float) -> str:
        """Move money into a savings goal.

        The only write in this module. Reached solely through the payday split,
        which refuses to run unless transfers are explicitly enabled and the
        amount is under a configured cap.
        """
        account_uid, _ = await self._resolve_account()
        goals = await self._get(f"/account/{account_uid}/savings-goals")
        target = None
        wanted = pot.strip().lower()
        for goal in goals.get("savingsGoalList") or []:
            if wanted in str(goal.get("name", "")).lower():
                target = goal
                break
        if target is None:
            raise SkillError(f"no savings goal matching {pot!r}")

        transfer_uid = str(uuid.uuid4())
        await self._request(
            "PUT",
            f"/account/{account_uid}/savings-goals/{target['savingsGoalUid']}"
            f"/add-money/{transfer_uid}",
            {"amount": {"currency": "GBP", "minorUnits": round(amount * 100)}},
        )
        return transfer_uid


def _amount(block: dict[str, Any] | None) -> float:
    """Starling counts in minor units — pence, not pounds."""
    if not block:
        return 0.0
    return round(int(block.get("minorUnits", 0)) / 100.0, 2)


def _transaction(item: dict[str, Any]) -> Transaction:
    amount = _amount(item.get("amount"))
    # The feed reports magnitudes and a direction, so the sign has to be
    # applied here — without it every outgoing looks like income.
    if str(item.get("direction", "")).upper() == "OUT":
        amount = -amount
    when = item.get("transactionTime") or item.get("updatedAt") or ""
    happened = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    return Transaction(
        id=str(item.get("feedItemUid") or uuid.uuid4()),
        happened_at=happened,
        amount=amount,
        merchant=str(item.get("counterPartyName") or ""),
        category=str(item.get("spendingCategory") or ""),
        source="starling",
    )
