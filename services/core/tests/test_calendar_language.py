"""Natural-language date parsing.

These phrasings come straight out of a voice transcript. Resolving them locally
is what keeps scheduling off the model's arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nova.integrations.calendar import parse_duration, parse_when

#: A fixed Wednesday afternoon, so "next Tuesday" has a stable answer.
NOW = datetime(2026, 7, 29, 14, 30)


def test_today_and_tomorrow() -> None:
    assert parse_when("tomorrow at 9am", now=NOW)[0].date() == NOW.date() + timedelta(days=1)
    assert parse_when("today at 5pm", now=NOW)[0].date() == NOW.date()
    assert parse_when("the day after tomorrow at 10", now=NOW)[0].date() == NOW.date() + timedelta(
        days=2
    )


def test_relative_offsets() -> None:
    when, all_day = parse_when("in 20 minutes", now=NOW)
    assert when == NOW + timedelta(minutes=20)
    assert all_day is False
    assert parse_when("in 2 hours", now=NOW)[0] == NOW + timedelta(hours=2)
    assert parse_when("in 3 days", now=NOW)[0] == NOW + timedelta(days=3)


def test_weekdays_resolve_forward() -> None:
    friday, _ = parse_when("Friday at 6pm", now=NOW)
    assert friday.weekday() == 4
    assert friday > NOW

    # NOW is a Wednesday; "Wednesday" means the next one, never today.
    wednesday, _ = parse_when("Wednesday at 10am", now=NOW)
    assert wednesday.date() == NOW.date() + timedelta(days=7)


def test_next_weekday_skips_a_week() -> None:
    plain, _ = parse_when("Tuesday at 9am", now=NOW)
    explicit, _ = parse_when("next Tuesday at 9am", now=NOW)
    assert explicit > plain


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("at 3pm", (15, 0)),
        ("at 15:30", (15, 30)),
        ("at 9am", (9, 0)),
        ("at midnight", (0, 0)),
        ("at noon", (12, 0)),
        ("half past four", (16, 30)),
        ("half four", (16, 30)),
        ("quarter past six", (18, 15)),
        ("quarter to seven", (18, 45)),
        ("this evening", (19, 0)),
    ],
)
def test_spoken_clock_forms(phrase: str, expected: tuple[int, int]) -> None:
    when, all_day = parse_when(f"tomorrow {phrase}", now=NOW)
    assert (when.hour, when.minute) == expected
    assert all_day is False


def test_bare_hours_assume_the_sensible_half_of_the_clock() -> None:
    """'Meet me at 6' means the evening, not six in the morning."""
    when, _ = parse_when("tomorrow at 6", now=NOW)
    assert when.hour == 18
    # …but an explicit am wins.
    assert parse_when("tomorrow at 6am", now=NOW)[0].hour == 6


def test_a_time_already_past_rolls_to_tomorrow() -> None:
    """At 14:30, 'at 9am' cannot mean earlier today."""
    when, _ = parse_when("at 9am", now=NOW)
    assert when.date() == NOW.date() + timedelta(days=1)
    assert when.hour == 9


def test_an_explicit_date_is_never_rolled_forward() -> None:
    when, _ = parse_when("today at 9am", now=NOW)
    assert when.date() == NOW.date()


def test_iso_and_slash_dates() -> None:
    assert parse_when("2026-12-25 at 10am", now=NOW)[0].date() == datetime(2026, 12, 25).date()
    assert parse_when("25/12 at 10am", now=NOW)[0].date() == datetime(2026, 12, 25).date()


def test_a_day_without_a_time_is_all_day() -> None:
    when, all_day = parse_when("tomorrow", now=NOW)
    assert all_day is True
    assert when.date() == NOW.date() + timedelta(days=1)


def test_durations() -> None:
    assert parse_duration("for 30 minutes") == 30
    assert parse_duration("for 2 hours") == 120
    assert parse_duration("a meeting") == 60  # default
    assert parse_duration("45 min") == 45


def test_duration_is_not_mistaken_for_a_clock_time() -> None:
    """'in 20 minutes' must not parse 20 as an hour."""
    when, _ = parse_when("in 20 minutes", now=NOW)
    assert when == NOW + timedelta(minutes=20)
