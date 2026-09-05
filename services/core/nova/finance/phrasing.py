"""Turning figures into the sentence that gets spoken.

Every string N.O.V.A. says about money is built here, from numbers, by code.
That is the point of the module: the alternative is handing a balance to a
model and asking it to phrase the answer, which puts the balance in a prompt
and lets the wording drift into a verdict.

The affordability line is the shape the brief specifies, near enough word for
word, because it is a good shape: what you have, how long it has to last, what
the spend would leave, and what that is per day. No adjectives. Nothing that
resolves to yes or no.
"""

from __future__ import annotations

from .budget import Affordability


def money(amount: float) -> str:
    """`£340`, or `£12.70` — pounds lose the pence, parts keep them.

    Whole numbers are how people say amounts aloud, and "three hundred and
    forty pounds zero pence" is nobody's idea of an answer.
    """
    rounded = round(amount, 2)
    sign = "-" if rounded < 0 else ""
    value = abs(rounded)
    if value % 1 == 0:
        return f"{sign}£{value:,.0f}"
    return f"{sign}£{value:,.2f}"


def days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def affordability(result: Affordability) -> str:
    """The fixed answer to "what have I got" and "can I afford X".

    Never says yes and never says no — the figures are the answer, and the
    person holding the card is better placed to weigh them than a program they
    can edit.
    """
    head = f"{money(result.available)} available. {days(result.days_until_payday)} until payday."

    if result.spend <= 0:
        return f"{head} That's {money(result.per_day)} a day."

    return (
        f"{head} That spend leaves {money(result.remaining_after_spend)}, "
        f"which is {money(result.per_day)} a day."
    )


def committed_detail(result: Affordability) -> str:
    """What is still due before payday, when someone asks why the figure is low."""
    if not result.outgoings_due:
        return f"Nothing committed before payday. Balance is {money(result.balance)}."
    parts = ", ".join(f"{o.name} {money(o.amount)}" for o in result.outgoings_due)
    return (
        f"{money(result.balance)} in the account, {money(result.committed)} committed "
        f"before payday: {parts}."
    )


def large_spend(merchant: str, amount: float, balance: float) -> str:
    """The alert. Merchant, amount, remaining balance — the brief allows nothing else."""
    where = merchant.strip() or "an unnamed merchant"
    return f"{money(abs(amount))} at {where}. {money(balance)} left."


def cooling_off_prompt(item: str, amount: float, would_leave: float) -> str:
    return (
        f"You wanted {item} for {money(amount)}. "
        f"Buying it now would leave {money(would_leave)}. "
        f"Bought it, dropped it, or still thinking?"
    )


def queue_summary(items: list[tuple[str, float]]) -> str:
    if not items:
        return "Nothing waiting."
    total = sum(amount for _, amount in items)
    listed = ", ".join(f"{item} {money(amount)}" for item, amount in items)
    noun = "item" if len(items) == 1 else "items"
    return f"{len(items)} {noun} waiting, {money(total)} in total: {listed}."


def dropped_summary(count: int, total: float, since_label: str) -> str:
    """The number worth surfacing: what not buying things has been worth."""
    if count == 0:
        return f"Nothing dropped {since_label}."
    noun = "purchase" if count == 1 else "purchases"
    return f"{count} {noun} dropped {since_label}, worth {money(total)}."


def transfer_done(amount: float, destination: str, *, dry_run: bool) -> str:
    if dry_run:
        return f"Dry run: would move {money(amount)} into {destination}. Nothing was transferred."
    return f"Moved {money(amount)} into {destination}."
