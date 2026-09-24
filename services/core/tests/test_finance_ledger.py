"""The finance database and the statement importer.

The importer is the brief's starting point because it carries no credential
risk and makes everything above it testable offline. That only holds if it
reads the files banks actually produce, so the fixtures here use the header
spellings and formats real exports use rather than an idealised one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nova.finance.adapters.csv_import import CsvAdapter, parse
from nova.finance.ledger import Ledger
from nova.runtime.errors import SkillError

STARLING_EXPORT = """Date,Counter Party,Reference,Type,Amount (GBP),Balance (GBP)
14/09/2026,Tesco,GROCERIES,FASTER PAYMENT,-42.50,957.50
13/09/2026,Employer Ltd,SALARY,FASTER PAYMENT,1000.00,1000.00
"""


@pytest.fixture
async def ledger(tmp_path: Path) -> Ledger:
    book = Ledger(tmp_path / "finance.db")
    await book.open()
    return book


# ------------------------------------------------------------------ importer


def test_it_reads_a_real_export_shape() -> None:
    rows = parse(STARLING_EXPORT)

    assert len(rows) == 2
    assert rows[0].merchant == "Tesco"
    assert rows[0].amount == -42.50
    assert rows[0].happened_at.date().isoformat() == "2026-09-14"
    assert rows[1].amount == 1000.00


def test_money_out_stays_negative_and_money_in_stays_positive() -> None:
    """The single most consequential thing the importer does. A sign flipped
    here turns every outgoing into income and the affordability figure into
    fiction."""
    rows = parse(STARLING_EXPORT)

    assert rows[0].is_debit
    assert not rows[1].is_debit


def test_brackets_mean_negative() -> None:
    text = "Date,Description,Amount\n2026-09-14,Tesco,(42.50)\n"
    assert parse(text)[0].amount == -42.50


def test_currency_symbols_and_thousands_separators_are_tolerated() -> None:
    text = 'Date,Description,Amount\n2026-09-14,Rent,"£-1,250.00"\n'
    assert parse(text)[0].amount == -1250.00


@pytest.mark.parametrize(
    "written",
    ["2026-09-14", "14/09/2026", "14 Sep 2026", "2026-09-14T08:30:00"],
)
def test_the_usual_date_formats_all_read(written: str) -> None:
    text = f"Date,Description,Amount\n{written},Tesco,-1.00\n"
    assert parse(text)[0].happened_at.date().isoformat() == "2026-09-14"


def test_the_same_row_gets_the_same_id_every_time() -> None:
    """Re-importing a statement must not double-count, and no bank export
    carries an id to rely on."""
    first = parse(STARLING_EXPORT)
    second = parse(STARLING_EXPORT)

    assert [t.id for t in first] == [t.id for t in second]


def test_two_identical_purchases_on_one_day_stay_distinct() -> None:
    """Buying the same coffee twice is not a duplicate row, and hashing only
    the visible fields would silently merge them."""
    text = "Date,Description,Amount\n2026-09-14,Coffee,-3.00\n2026-09-14,Coffee,-3.00\n"
    rows = parse(text)

    assert len({t.id for t in rows}) == 2


def test_a_file_with_no_usable_columns_says_so() -> None:
    with pytest.raises(SkillError, match="date column"):
        parse("Nonsense,Headers\n1,2\n")


def test_trailing_blank_lines_are_ignored() -> None:
    assert len(parse(STARLING_EXPORT + "\n\n")) == 2


async def test_the_adapter_reports_the_closing_balance_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "statement.csv"
    path.write_text(STARLING_EXPORT, encoding="utf-8")
    adapter = CsvAdapter(path)

    assert adapter.load() == 2
    assert adapter.balance_known
    assert (await adapter.balance()).effective == 957.50


async def test_the_closing_balance_is_the_newest_row_whichever_end_it_is_at(
    tmp_path: Path,
) -> None:
    """Banks export newest-first as often as oldest-first. Taking the last row
    means taking the oldest balance in half of all statements — the balance
    from before everything in the file happened."""
    oldest_first = """Date,Counter Party,Amount (GBP),Balance (GBP)
13/09/2026,Employer Ltd,1000.00,1000.00
14/09/2026,Tesco,-42.50,957.50
"""
    path = tmp_path / "oldest-first.csv"
    path.write_text(oldest_first, encoding="utf-8")
    adapter = CsvAdapter(path)
    adapter.load()

    assert (await adapter.balance()).effective == 957.50


async def test_without_a_balance_column_it_sums_and_says_it_is_guessing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "statement.csv"
    path.write_text("Date,Description,Amount\n2026-09-14,Tesco,-42.50\n", encoding="utf-8")
    adapter = CsvAdapter(path)
    adapter.load()

    assert not adapter.balance_known
    assert (await adapter.balance()).effective == -42.50


# -------------------------------------------------------------------- ledger


async def test_transactions_are_stored_once(ledger: Ledger) -> None:
    rows = parse(STARLING_EXPORT)

    assert await ledger.record_transactions(rows) == 2
    assert await ledger.record_transactions(rows) == 0, "a re-import must not double up"


async def test_spend_counts_only_money_going_out(ledger: Ledger) -> None:
    await ledger.record_transactions(parse(STARLING_EXPORT))

    spent = await ledger.spend_since(datetime(2026, 1, 1, tzinfo=UTC))

    assert spent == 42.50, "the £1000 salary is not spending"


async def test_an_event_can_only_be_claimed_once(ledger: Ledger) -> None:
    """The webhook dedupe. Banks retry until acknowledged, so the same
    transaction arrives repeatedly as a matter of course."""
    assert await ledger.claim_event("evt-1") is True
    assert await ledger.claim_event("evt-1") is False
    assert await ledger.claim_event("evt-2") is True


async def test_the_cooling_off_queue_remembers_and_decides(ledger: Ledger) -> None:
    now = datetime.now(UTC)
    first = await ledger.add_pending("headphones", 200.0, now - timedelta(hours=1))
    await ledger.add_pending("a coat", 80.0, now + timedelta(hours=48))

    assert len(await ledger.pending()) == 2
    assert [p.item for p in await ledger.due_to_ask()] == ["headphones"]

    await ledger.decide(first, "dropped")

    assert [p.item for p in await ledger.pending()] == ["a coat"]
    count, total = await ledger.dropped_since(now - timedelta(days=1))
    assert (count, total) == (1, 200.0)


async def test_asking_is_recorded_so_it_only_happens_once(ledger: Ledger) -> None:
    now = datetime.now(UTC)
    item = await ledger.add_pending("headphones", 200.0, now - timedelta(hours=1))

    await ledger.mark_asked(item)

    assert await ledger.due_to_ask() == [], "it should not be asked about twice"


async def test_still_thinking_puts_it_back_in_the_queue(ledger: Ledger) -> None:
    now = datetime.now(UTC)
    item = await ledger.add_pending("headphones", 200.0, now - timedelta(hours=1))
    await ledger.mark_asked(item)

    await ledger.requeue(item, now - timedelta(minutes=1))

    assert [p.item for p in await ledger.due_to_ask()] == ["headphones"]


async def test_transfers_are_logged_with_what_they_were(ledger: Ledger) -> None:
    await ledger.record_transfer(150.0, "Savings", dry_run=True, trigger="payday")

    logged = await ledger.transfers()

    assert logged[0]["amount"] == 150.0
    assert logged[0]["destination"] == "Savings"
    assert logged[0]["dry_run"] == 1


async def test_the_database_is_not_readable_by_anyone_else(ledger: Ledger) -> None:
    assert ledger.path.stat().st_mode & 0o077 == 0
