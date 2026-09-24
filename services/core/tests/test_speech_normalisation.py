"""What gets said, as opposed to what gets written.

The two are not the same string and the difference is not cosmetic. "£8.63" is
right on a screen and wrong in an ear — read literally it comes out as "pound
eight point six three", which is not how anybody says a price and is oddly hard
to follow when it is your own money being read back to you.

This is the layer that fixes it, for every skill at once: the alternative is
each one keeping a spoken copy of every sentence, which drifts.
"""

from __future__ import annotations

import pytest

from nova.voice.tts import normalise_for_speech


@pytest.mark.parametrize(
    "written,spoken",
    [
        # The reported bug: the symbol read where it sits, and a decimal point
        # that is not a decimal point.
        ("£8.63", "8 pounds 63"),
        ("£12.70", "12 pounds 70"),
        ("£10.77", "10 pounds 77"),
        # Whole pounds lose the pence rather than saying "point zero zero".
        ("£8.00", "8 pounds"),
        ("£340", "340 pounds"),
        # Singulars.
        ("£1", "1 pound"),
        ("£1.00", "1 pound"),
        ("£0.01", "1 penny"),
        # Pence only.
        ("£0.63", "63 pence"),
        # Thousands separators are punctuation, not something to read out.
        ("£1,250.00", "1250 pounds"),
        ("£1,250.50", "1250 pounds 50"),
        # Negative amounts, which is how an overdraft arrives.
        ("-£20", "minus 20 pounds"),
        ("-£20.40", "minus 20 pounds 40"),
    ],
)
def test_money_is_said_the_way_people_say_it(written: str, spoken: str) -> None:
    assert normalise_for_speech(written) == spoken


def test_a_trailing_zero_in_the_pence_is_tens_not_units() -> None:
    """`£1.5` is one pound fifty. Reading the pence field as a number rather
    than padding it would say "one pound five" — a tenfold error, in the
    direction of sounding cheaper than it is."""
    assert normalise_for_speech("£1.5") == "1 pound 50"
    assert normalise_for_speech("£1.50") == "1 pound 50"


def test_single_pence_are_spelled_out_to_avoid_a_tenfold_ambiguity() -> None:
    """ "Eight pounds five" is heard as £8.50. Anything under ten pence gets the
    word, because the shorthand only works for two-digit pence."""
    assert normalise_for_speech("£8.05") == "8 pounds and 5 pence"
    assert normalise_for_speech("£8.50") == "8 pounds 50"


def test_a_whole_sentence_survives_intact() -> None:
    said = normalise_for_speech(
        "£340 available. 13 days until payday. That spend leaves £8.63, which is £1.50 a day."
    )

    assert said == (
        "340 pounds available. 13 days until payday. That spend leaves 8 pounds 63, "
        "which is 1 pound 50 a day."
    )
    assert "£" not in said
    assert "point" not in said


def test_the_advice_sentence_has_nothing_left_to_mispronounce() -> None:
    """The seam between the finance module's wording and the voice: the one
    builds sentences with £ in them, the other has to say them."""
    said = normalise_for_speech(
        "A Nintendo Switch at £300 is a want rather than a need. It leaves £140 "
        "for the 13 days to payday, which is £10.77 a day — doable, not comfortable."
    )

    assert "£" not in said
    assert "300 pounds" in said
    assert "10 pounds 77" in said
    # And "Nintendo Switch" is left alone.
    assert "Nintendo Switch" in said


# ------------------------------------------ the rules that were already here


@pytest.mark.parametrize(
    "written,expected",
    [
        ("72%", "72 percent"),
        ("21°C", "21 degrees"),
        ("512 MB", "512 megabytes"),
        ("**bold**", "bold"),
        ("N.O.V.A.", "Nova"),
        ("CPU", "C P U"),
    ],
)
def test_the_existing_substitutions_still_apply(written: str, expected: str) -> None:
    """Money is a new step in an existing pipeline; it must not have displaced
    anything on its way in."""
    assert normalise_for_speech(written) == expected
