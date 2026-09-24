"""What a call says before it says anything else.

An outbound call has one job in its first sentence: explain itself. A phone
ringing at nine in the evening and opening with "hello?" is a nuisance call
from your own house — the person answering has to work out who it is and why
before they can think about the question.

So the opener names the time of day, the caller, the reason, and the question,
in that order, and stops. It is built here from the transaction rather than
written by a model, for the same reason every other spoken figure is: this
sentence contains a merchant and an amount, and the point of the finance module
is that those do not travel to anyone who does not need them.
"""

from __future__ import annotations

from datetime import datetime

from ..finance.phrasing import money


def time_of_day(now: datetime) -> str:
    hour = now.hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def greeting(now: datetime, honorific: str = "") -> str:
    """`Good evening sir` — or just `Good evening` if nobody wants to be sir."""
    address = honorific.strip()
    return f"{time_of_day(now)} {address}" if address else time_of_day(now)


def large_spend_opening(merchant: str, amount: float, *, now: datetime, honorific: str = "") -> str:
    """The reason the phone rang, said first.

    Deliberately a question about the transaction and not about the person.
    "Was this you" is answerable; "is everything all right with your account"
    is alarming and tells them nothing.
    """
    where = merchant.strip() or "an unnamed merchant"
    return (
        f"{greeting(now, honorific)}. I noticed a payment of {money(abs(amount))} "
        f"at {where}, and wanted to check it was you."
    )


def confirmed() -> str:
    """They said yes. Get out of the way."""
    return "Thank you. I'll leave it there unless you want anything else."


def disputed() -> str:
    """They said no.

    N.O.V.A. cannot freeze a card and does not pretend it can — the module is
    read-only apart from the payday transfer. What it can do is say who to ring
    and remember that this one was disputed.
    """
    return (
        "Understood. I've marked it as disputed. Ring Starling on the number on "
        "the back of your card, or through the app, and they can freeze it. "
        "I can't do that part myself."
    )


def unclear() -> str:
    """They said something that was neither."""
    return "Sorry — was that payment yours, or not?"


#: Read as "yes, that was me". Matched against the whole answer, since a phone
#: line and a transcriber together turn short words into other short words.
YES = frozenset(
    {
        "yes",
        "yes it was",
        "yes that was me",
        "that was me",
        "it was me",
        "yeah",
        "yeah that was me",
        "yep",
        "that's mine",
        "thats mine",
        "mine",
        "i did",
        "that was mine",
        "yes thanks",
    }
)

#: Read as "no, that was not me".
NO = frozenset(
    {
        "no",
        "no it wasn't",
        "no it wasnt",
        "that wasn't me",
        "that wasnt me",
        "not me",
        "no that wasn't me",
        "no that wasnt me",
        "nope",
        "i didn't",
        "i didnt",
        "no i didn't",
        "not mine",
        "that's not mine",
        "thats not mine",
    }
)


def reading(answer: str) -> str:
    """`yes`, `no`, or `unclear`.

    Whole-answer matching. "No, that was me" contains "no" and means yes, so
    searching for the word rather than reading the answer gets it backwards —
    on the one question where being backwards matters most.
    """
    cleaned = answer.strip().lower().rstrip(".!?").replace(",", "")
    if cleaned in YES:
        return "yes"
    if cleaned in NO:
        return "no"
    return "unclear"
