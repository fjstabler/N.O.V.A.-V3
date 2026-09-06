"""Turning figures into the sentence that gets spoken.

Every string N.O.V.A. says about money is built here, from numbers, by code.
That is the point of the module: the alternative is handing a balance to a
model and asking it to phrase the answer, which puts the balance in a prompt
and lets the wording drift into a verdict.

The affordability line is the shape the brief specifies, near enough word for
word, because it is a good shape: what you have, how long it has to last, what
the spend would leave, and what that is per day.

`advice` and `comfort` at the bottom go further and recommend something, which
the brief forbade and the owner has since asked for. They are still built here
from figures rather than by a model, so what gets said is the same every time
and no balance is ever sent anywhere to be phrased.
"""

from __future__ import annotations

from datetime import date

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


def on_the(day: date) -> str:
    """`the 25th` — how a date gets said out loud."""
    number = day.day
    suffix = "th" if 11 <= number <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"the {number}{suffix}"


def _opening(item: str, kind: str, price: str) -> str:
    """ "A Nintendo Switch at £300 is a want rather than a need".

    First letter raised rather than `.capitalize()`, which lowercases the rest
    and turns every brand name into a common noun — "Nintendo switch",
    "Iphone", "Dr martens".
    """
    what = item.strip() or "that"
    what = what[0].upper() + what[1:]
    tail = "a necessity" if kind == "need" else "a want rather than a need"
    return f"{what} at {price} is {tail}"


def advice(item: str, result: Affordability, *, kind: str, verdict: str) -> str:
    """The recommendation, in the shape somebody actually asks for it.

    Reason, then figures, then what it would do — and it says "if it were me",
    because that is honestly what this is. A rule of thumb applied to a
    balance, not a fact about whether the purchase is wise.

    A necessity is never told to wait, even when it goes overdrawn. "Put off
    the boiler repair" is not advice, it is a burst pipe; what is useful there
    is knowing the overdraft is coming.
    """
    price = money(result.spend)
    left = money(result.remaining_after_spend)
    window = f"the {days(result.days_until_payday)} to payday"
    rate = f"{money(result.per_day)} a day"
    payday = on_the(result.payday)
    opening = _opening(item, kind, price)

    if verdict == "overdrawn":
        short = money(abs(result.remaining_after_spend))
        if kind == "need":
            return (
                f"{opening}, but it would put you {short} overdrawn before payday on "
                f"{payday}. Worth seeing what else can wait, or whether it can be "
                "split up."
            )
        return (
            f"{opening}, and it would put you {short} overdrawn before payday on "
            f"{payday}. If it were me I'd wait."
        )
    if verdict == "unavoidable":
        return (
            f"{opening}, so it is less of a choice. It leaves {left} for {window}, "
            f"which is {rate} — tight. Worth seeing what else can wait."
        )
    if verdict == "wait":
        return (
            f"{opening}, and it leaves {left} for {window} — {rate}. "
            f"If it were me I'd wait for payday on {payday}."
        )
    if verdict == "sleep_on_it":
        return (
            f"{opening}. It leaves {left} for {window}, which is {rate} — doable, "
            "not comfortable. If it were me I'd sleep on it. Say you want to buy it "
            "and I'll ask you again in a couple of days."
        )
    return f"{opening}, and it still leaves {left} for {window} — {rate}. If it were me I'd get it."


def comfort(verdict: str, _result: Affordability) -> str:
    """The judgement appended to "can I afford £20".

    Short, and it repeats no figure: the sentence in front of it has just said
    what the spend leaves and what that is per day. This answers the question
    actually being asked, which is not "will the card work" but "does that
    leave me enough room until payday".
    """
    if verdict == "overdrawn":
        return "That would take you overdrawn before payday."
    if verdict in ("wait", "unavoidable"):
        return "That is tight for the time left."
    if verdict == "sleep_on_it":
        return "That works, but there is not much room in it."
    return "That is comfortable, with room to spare."
