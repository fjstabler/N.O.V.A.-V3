"""Reasoning-tier routing: which turns escalate to Claude.

The rule the tests pin down: nothing escalates unless the advanced tier is
configured; an explicit ask overrides the auto-route setting; and the keyword
heuristics stay conservative — an everyday spoken command ("turn off the
kitchen light") is not clever enough to warrant the stronger, slower model.
"""

from __future__ import annotations

import pytest

from nova.ai.router import wants_advanced


def advanced(text: str, *, auto_route: bool = True) -> bool:
    return wants_advanced(text, configured=True, auto_route=auto_route)


# ----------------------------------------------------------- gating on config


def test_nothing_escalates_when_the_tier_is_not_configured() -> None:
    # Even an explicit "use claude" can't route to a tier with no key.
    assert (
        wants_advanced("use claude to write a python script", configured=False, auto_route=True)
        is False
    )


def test_everyday_commands_stay_on_the_fast_tier() -> None:
    for command in [
        "turn off the kitchen light",
        "what's the weather like",
        "set a timer for ten minutes",
        "show me the bedroom",
        "watch my room",
        "open the front door",  # a camera/home request — not system control
    ]:
        assert advanced(command) is False, command


# ------------------------------------------------------------- explicit asks


def test_an_explicit_request_escalates_even_with_auto_route_off() -> None:
    assert advanced("think hard about this one", auto_route=False) is True
    assert advanced("use claude for this", auto_route=False) is True


def test_auto_route_off_keeps_heuristic_matches_on_the_fast_tier() -> None:
    # A coding request would normally escalate, but with auto-route off only an
    # explicit ask does.
    assert advanced("debug this python function", auto_route=False) is False


# --------------------------------------------------------------- heuristics


def test_coding_requests_escalate() -> None:
    for command in [
        "write a python function to sort a list",
        "there's a syntax error in my script",
        "help me refactor this code",
        "explain this stack trace",
        "write a regex for email addresses",
    ]:
        assert advanced(command) is True, command


def test_system_control_requests_escalate() -> None:
    for command in [
        "create a folder called projects on my computer",
        "delete the file report.txt",
        "run the command ls in the terminal",
        "install docker on my machine",
    ]:
        assert advanced(command) is True, command


def test_complex_multi_step_requests_escalate() -> None:
    for command in [
        "walk me through setting up a web server step by step",
        "make a plan for migrating my database",
        "compare and contrast postgres and mysql",
    ]:
        assert advanced(command) is True, command


def test_a_long_elaborate_request_escalates_on_length_alone() -> None:
    long_request = " ".join(f"word{i}" for i in range(45))
    assert advanced(long_request) is True


def test_a_short_request_with_no_signals_does_not() -> None:
    assert advanced("remind me to call mum") is False


@pytest.mark.parametrize("auto_route", [True, False])
def test_configured_but_unconfigured_gate_is_independent_of_auto_route(auto_route: bool) -> None:
    assert wants_advanced("write code", configured=False, auto_route=auto_route) is False
