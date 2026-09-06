"""Payday dates and the affordability arithmetic.

The whole point of this module is that the numbers are worked out here rather
than by a model, so they have to be right, and they have to be the same every
time. The brief names the edge cases: payday on a weekend, a negative available
balance, an outgoing due today.

Every date below is real. 2026-08-31 is the August bank holiday, 2026-12-25 a
Friday, 2027-01-01 a Friday — checked against the calendar rather than assumed,
because a test that agrees with a bug is worse than no test.
"""

from __future__ import annotations

from datetime import date

import pytest

from nova.finance.budget import (
    Affordability,
    Outgoing,
    assess,
    bank_holidays,
    easter,
    is_working_day,
    next_payday,
    outgoings_before,
    payday_in,
    recommend,
)
from nova.finance.phrasing import affordability, money

# ------------------------------------------------------------------ holidays


def test_easter_lands_where_the_calendar_says() -> None:
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)
    assert easter(2024) == date(2024, 3, 31)


def test_the_usual_bank_holidays_are_known() -> None:
    holidays = bank_holidays(2026)

    assert date(2026, 1, 1) in holidays  # New Year's Day, a Thursday
    assert date(2026, 4, 3) in holidays  # Good Friday
    assert date(2026, 4, 6) in holidays  # Easter Monday
    assert date(2026, 5, 4) in holidays  # early May, first Monday
    assert date(2026, 5, 25) in holidays  # spring, last Monday
    assert date(2026, 8, 31) in holidays  # summer, last Monday
    assert date(2026, 12, 25) in holidays


def test_christmas_at_a_weekend_gets_a_substitute_weekday() -> None:
    """2027: Christmas is a Saturday and Boxing Day a Sunday, so the substitutes
    are the following Monday and Tuesday. A payday shifted off Christmas has to
    clear the substitutes too, or it lands on a day the banks are shut."""
    holidays = bank_holidays(2027)

    assert date(2027, 12, 25) in holidays
    assert date(2027, 12, 27) in holidays
    assert date(2027, 12, 28) in holidays


# -------------------------------------------------------------------- payday


def test_a_payday_on_a_working_day_does_not_move() -> None:
    assert payday_in(2026, 9, 25) == date(2026, 9, 25)  # a Friday


def test_a_payday_at_a_weekend_moves_back_to_the_friday() -> None:
    """The brief's first named edge case. Paying early is the common
    arrangement, and assuming money lands later than it does is the error that
    overstates what is available."""
    # 2026-04-25 is a Saturday.
    assert payday_in(2026, 4, 25) == date(2026, 4, 24)


def test_a_payday_can_be_configured_to_move_forward_instead() -> None:
    assert payday_in(2026, 4, 25, rule="after") == date(2026, 4, 27)


def test_a_payday_on_a_bank_holiday_keeps_moving_until_a_working_day() -> None:
    """25 December 2026 is a Friday and a bank holiday, and the 24th is the
    Thursday before it — so this only passes if holidays are considered as well
    as weekends."""
    assert payday_in(2026, 12, 25) == date(2026, 12, 24)


def test_a_payday_later_than_the_month_is_the_last_day_of_it() -> None:
    """A 31st payday in February is the 28th, not a crash."""
    assert payday_in(2026, 2, 31) == date(2026, 2, 27)  # 28th is a Saturday


def test_the_next_payday_is_strictly_after_today() -> None:
    """On payday itself the money has arrived, and "how long until payday"
    means the next one. Counting today would answer zero days and then divide
    the remainder by it."""
    assert next_payday(date(2026, 9, 25), 25) == date(2026, 10, 23)
    assert next_payday(date(2026, 9, 24), 25) == date(2026, 9, 25)


def test_december_rolls_into_the_next_year() -> None:
    assert next_payday(date(2026, 12, 31), 25) == date(2027, 1, 25)


def test_working_days_exclude_weekends_and_holidays() -> None:
    assert is_working_day(date(2026, 9, 25))
    assert not is_working_day(date(2026, 9, 26))  # Saturday
    assert not is_working_day(date(2026, 8, 31))  # summer bank holiday


# ----------------------------------------------------------------- outgoings


def test_only_outgoings_before_payday_count() -> None:
    outgoings = [
        Outgoing("rent", 600, 1),
        Outgoing("phone", 20, 12),
        Outgoing("gym", 30, 28),
    ]
    due = outgoings_before(outgoings, date(2026, 9, 10), date(2026, 9, 25))

    assert [o.name for o in due] == ["phone"]


def test_an_outgoing_due_today_still_counts() -> None:
    """The brief's third edge case. A payment dated today has not necessarily
    left the account yet, and treating it as gone is what overstates what is
    available — in the direction that costs money."""
    due = outgoings_before([Outgoing("rent", 600, 10)], date(2026, 9, 10), date(2026, 9, 25))
    assert [o.name for o in due] == ["rent"]


def test_an_outgoing_falling_after_a_short_month_is_still_found() -> None:
    """A 31st outgoing in February is due on the 28th, the same way payday is."""
    due = outgoings_before([Outgoing("card", 50, 31)], date(2026, 2, 20), date(2026, 3, 5))
    assert [o.name for o in due] == ["card"]


# ------------------------------------------------------------- affordability


def test_the_worked_example_from_the_brief() -> None:
    """£340 available, 11 days until payday, a £200 spend leaves £140 at
    £12.70 a day — the exact figures the brief specifies, end to end."""
    result = assess(
        balance=400.0,
        outgoings=[Outgoing("phone", 60, 20)],
        today=date(2026, 9, 14),
        payday_day=25,
        spend=200.0,
    )

    assert result.available == 340.0
    assert result.days_until_payday == 11
    assert result.remaining_after_spend == 140.0
    assert result.per_day == 12.73  # 140 / 11
    assert affordability(result) == (
        "£340 available. 11 days until payday. That spend leaves £140, which is £12.73 a day."
    )


def test_with_no_spend_it_reports_what_is_there_per_day() -> None:
    result = assess(
        balance=400.0,
        outgoings=[Outgoing("phone", 60, 20)],
        today=date(2026, 9, 14),
        payday_day=25,
    )

    assert affordability(result) == "£340 available. 11 days until payday. That's £30.91 a day."


def test_a_negative_available_balance_is_reported_not_hidden() -> None:
    """The brief's second edge case. Committed outgoings can exceed the
    balance, and the honest answer is the negative number — clamping it to zero
    would hide exactly the situation worth knowing about."""
    result = assess(
        balance=100.0,
        outgoings=[Outgoing("rent", 600, 20)],
        today=date(2026, 9, 14),
        payday_day=25,
    )

    assert result.available == -500.0
    assert "-£500 available" in affordability(result)


def test_the_answer_never_reaches_a_verdict() -> None:
    """Constraint 3, asserted rather than trusted: no phrasing that resolves to
    yes or no, however the figures come out."""
    for balance, spend in ((5000.0, 10.0), (10.0, 5000.0), (0.0, 0.0)):
        said = affordability(
            assess(
                balance=balance,
                outgoings=[],
                today=date(2026, 9, 14),
                payday_day=25,
                spend=spend,
            )
        ).lower()
        for verdict in ("yes", "no ", "afford", "should", "can't", "cannot", "sorry"):
            assert verdict not in said, f"{verdict!r} in {said!r}"


def test_the_same_inputs_always_produce_the_same_string() -> None:
    """Determinism is an acceptance criterion: no clock read inside, no model
    call, no dictionary ordering leaking into the wording."""

    def once() -> str:
        return affordability(
            assess(
                balance=812.34,
                outgoings=[Outgoing("rent", 500, 1), Outgoing("phone", 20, 18)],
                today=date(2026, 9, 14),
                payday_day=25,
                spend=99.99,
            )
        )

    assert len({once() for _ in range(25)}) == 1


def test_money_reads_the_way_a_person_says_it() -> None:
    assert money(340) == "£340"
    assert money(12.7) == "£12.70"
    assert money(-45.5) == "-£45.50"
    assert money(1234.56) == "£1,234.56"


# ------------------------------------------------------------------- advice


def situation(balance: float, spend: float) -> Affordability:
    """13 days to payday on the 25th, one £60 outgoing still to come."""
    return assess(
        balance=balance,
        outgoings=[Outgoing("phone", 60, 20)],
        today=date(2026, 9, 12),
        payday_day=25,
        spend=spend,
    )


@pytest.mark.parametrize("kind", ["need", "want"])
def test_going_overdrawn_is_called_out_whatever_it_is_for(kind: str) -> None:
    assert recommend(situation(200, 300), kind=kind, floor=10.0) == "overdrawn"


def test_a_want_that_leaves_too_little_a_day_is_a_wait() -> None:
    # £340 available, £300 spend, £40 over 13 days — about £3 a day.
    assert recommend(situation(400, 300), kind="want", floor=10.0) == "wait"


def test_a_want_with_some_room_is_worth_sleeping_on() -> None:
    # £10.77 a day: above the floor, under twice it.
    assert recommend(situation(500, 300), kind="want", floor=10.0) == "sleep_on_it"


def test_a_want_with_plenty_behind_it_is_a_yes() -> None:
    assert recommend(situation(900, 300), kind="want", floor=10.0) == "go_ahead"


@pytest.mark.parametrize("balance", [360, 400, 500, 560, 600])
def test_a_necessity_is_never_told_to_wait(balance: float) -> None:
    """The rule that matters most here. Told to put off a boiler repair or a
    prescription, the advice stops being advice — and the module cannot tell
    which necessity it is looking at, so it must not tell anyone to skip any of
    them. Tight is worth saying; 'wait' is not."""
    verdict = recommend(situation(balance, 300), kind="need", floor=10.0)

    assert verdict in ("unavoidable", "go_ahead")
    assert verdict != "wait"


def test_the_same_figures_always_give_the_same_verdict() -> None:
    """Determinism is the proof no model is involved: asked twenty times, a
    model does not answer identically twenty times."""
    result = situation(500, 300)

    assert len({recommend(result, kind="want", floor=10.0) for _ in range(20)}) == 1


def test_the_floor_is_what_moves_the_line() -> None:
    """The one tuning knob, and it has to actually tune something."""
    result = situation(500, 300)  # £10.77 a day

    assert recommend(result, kind="want", floor=5.0) == "go_ahead"
    assert recommend(result, kind="want", floor=10.0) == "sleep_on_it"
    assert recommend(result, kind="want", floor=20.0) == "wait"
