"""Payday dates, committed outgoings, and what is actually left.

Every function here is pure and nothing in it calls a model. Most of it returns
numbers; `recommend` at the bottom returns a conclusion, which the brief
originally forbade and the owner has since asked for. The reasoning behind the
ban still holds — a verdict from a system its owner can reprogram is something
to argue with rather than a limit — so the recommendation is at least computed
here, from these numbers, deterministically, rather than by asking a model to
have an opinion about somebody's bank balance.

"Available" is the only figure that needs defining. It is the current balance
minus the outgoings still due before the next payday, so it answers "what can I
spend without missing a direct debit", which is the question people actually
have when they ask what they've got.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

#: England and Wales, generated rather than fetched — a finance module should
#: not need the internet to know that a Monday in August is a bank holiday.
#: Scotland and Northern Ireland differ; those are additions, not corrections.
_FIXED_HOLIDAYS = ((1, 1), (12, 25), (12, 26))


@dataclass(frozen=True, slots=True)
class Outgoing:
    """A committed payment: rent, a subscription, a standing order."""

    name: str
    amount: float
    day_of_month: int


@dataclass(frozen=True, slots=True)
class Affordability:
    """Everything the spoken answer needs, already worked out."""

    balance: float
    committed: float
    available: float
    payday: date
    days_until_payday: int
    spend: float
    remaining_after_spend: float
    per_day: float
    outgoings_due: tuple[Outgoing, ...]


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm — Good Friday and Easter Monday hang off it."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _last_monday(year: int, month: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - 0) % 7)


def bank_holidays(year: int) -> frozenset[date]:
    """England and Wales bank holidays for one year.

    Substitute days matter here: when Christmas falls at a weekend the
    substitute is the following weekday, and a payday moved off a bank holiday
    has to move off the substitute too.
    """
    days: set[date] = set()
    for month, day in _FIXED_HOLIDAYS:
        days.add(date(year, month, day))

    sunday = easter(year)
    days.add(sunday - timedelta(days=2))  # Good Friday
    days.add(sunday + timedelta(days=1))  # Easter Monday

    # Early May is the first Monday; spring and summer are last Mondays.
    may_first = date(year, 5, 1)
    days.add(may_first + timedelta(days=(0 - may_first.weekday()) % 7))
    days.add(_last_monday(year, 5))
    days.add(_last_monday(year, 8))

    # A fixed holiday landing at a weekend is observed on the next free weekday.
    for month, day in _FIXED_HOLIDAYS:
        holiday = date(year, month, day)
        if holiday.weekday() < 5:
            continue
        substitute = holiday
        while substitute.weekday() >= 5 or substitute in days:
            substitute += timedelta(days=1)
        days.add(substitute)

    return frozenset(days)


def is_working_day(day: date) -> bool:
    return day.weekday() < 5 and day not in bank_holidays(day.year)


def payday_in(year: int, month: int, day_of_month: int, *, rule: str = "before") -> date:
    """The payday for one month, moved off weekends and bank holidays.

    `rule` is "before" for employers who pay early rather than late, which is
    the common arrangement and the safe default: assuming money arrives later
    than it does produces an available balance that is too high.
    """
    last = calendar.monthrange(year, month)[1]
    payday = date(year, month, min(day_of_month, last))
    step = timedelta(days=-1 if rule != "after" else 1)
    while not is_working_day(payday):
        payday += step
    return payday


def next_payday(today: date, day_of_month: int, *, rule: str = "before") -> date:
    """The next payday strictly after `today`.

    Strictly after: on payday itself the money has landed, and the question
    "how long until I am paid" means the *next* one. Counting today would
    answer zero days and divide the remainder by nothing.
    """
    candidate = payday_in(today.year, today.month, day_of_month, rule=rule)
    if candidate > today:
        return candidate
    year = today.year + (today.month == 12)
    month = 1 if today.month == 12 else today.month + 1
    return payday_in(year, month, day_of_month, rule=rule)


def outgoings_before(
    outgoings: list[Outgoing] | tuple[Outgoing, ...], today: date, payday: date
) -> tuple[Outgoing, ...]:
    """Which committed payments still fall due between now and payday.

    Inclusive of today: an outgoing dated today has not necessarily left the
    account yet, and treating it as already gone is the error that overstates
    what is available.
    """
    due: list[Outgoing] = []
    for outgoing in outgoings:
        day = today
        while day <= payday:
            last = calendar.monthrange(day.year, day.month)[1]
            if day.day == min(outgoing.day_of_month, last):
                due.append(outgoing)
                break
            day += timedelta(days=1)
    return tuple(due)


def assess(
    *,
    balance: float,
    outgoings: list[Outgoing] | tuple[Outgoing, ...],
    today: date,
    payday_day: int,
    spend: float = 0.0,
    rule: str = "before",
) -> Affordability:
    """Work out what is available, and what a spend would leave.

    Deterministic by construction: same inputs, same numbers, no clock read and
    no network call inside.
    """
    payday = next_payday(today, payday_day, rule=rule)
    due = outgoings_before(outgoings, today, payday)
    committed = round(sum(o.amount for o in due), 2)
    available = round(balance - committed, 2)
    remaining = round(available - spend, 2)
    days = (payday - today).days
    # Guard the divide: `next_payday` is strictly after today, so days is at
    # least 1 — but a caller passing a hand-built date should not get a crash.
    per_day = round(remaining / days, 2) if days > 0 else remaining
    return Affordability(
        balance=round(balance, 2),
        committed=committed,
        available=available,
        payday=payday,
        days_until_payday=days,
        spend=round(spend, 2),
        remaining_after_spend=remaining,
        per_day=per_day,
        outgoings_due=due,
    )


# ------------------------------------------------------------------- advice

#: What the module is willing to conclude. Deliberately not a yes/no: "wait"
#: and "sleep on it" are the useful answers, and "you cannot" is not one of
#: them — it is somebody's own money and the card will work regardless.
Verdict = Literal["overdrawn", "unavoidable", "wait", "sleep_on_it", "go_ahead"]


def recommend(result: Affordability, *, kind: str, floor: float) -> Verdict:
    """Turn the figures into a recommendation, here rather than in a model.

    The brief forbade this outright and it was right about why: a verdict from
    a system its owner can reprogram is something to argue with rather than a
    limit. The owner asked for it anyway, which is their call — so it is done
    the way that keeps the rest of the guarantees intact. The arithmetic and
    the conclusion both happen on this machine, from these numbers, with no
    model involved; same inputs, same verdict, every time.

    What the model contributes is `kind`, its read on whether the thing is a
    need or a want. That judgement is about the item and not about the money,
    so making it requires no access to the account — which is the whole reason
    the split is drawn here.
    """
    if result.remaining_after_spend < 0:
        return "overdrawn"

    comfortable = result.per_day >= floor * 2
    if kind == "need":
        # Never told to skip a necessity. The useful thing to say about an
        # unavoidable purchase that leaves things tight is that it is tight,
        # not that they should go without medicine — and anything short of
        # comfortable is worth saying, since they are going to spend it either
        # way and the warning is the only part they can act on.
        return "go_ahead" if comfortable else "unavoidable"
    if result.per_day < floor:
        return "wait"
    if not comfortable:
        return "sleep_on_it"
    return "go_ahead"
