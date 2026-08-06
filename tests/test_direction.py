"""Money out, money in, and what is left.

A household wants to know three things from the same ledger, and the ways they
can silently disagree are all here: an inflow counted as negative spending, a
transfer between one's own accounts read as income, a "net" that quietly nets
off internal movement, and a trailing average drawn over periods that have not
happened yet.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.domain.models import DEPOSIT
from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord
from app.storage.sqlalchemy_repo import _bucket, _rolling, rolling_window

SHA = "d" * 64


def _seed(repository, context, amounts, *, day=3):
    """One document, one account, one row per amount."""
    account = AccountRecord(
        institution="Test", account_ref_masked="acct", sub_account_label="",
        currency="SGD", kind=DEPOSIT,
    )
    document = DocumentRecord(
        sha256=SHA, institution="Test", doc_type="acc",
        period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
        storage_path="x", parse_status="imported",
        source_profile="dummy", source_relpath="a.pdf",
        fetched_at=datetime.now(timezone.utc),
    )
    txns = [
        TxnRecord(
            account_key=account, posted_date=date(2026, 6, day), amount_minor=amount,
            currency="SGD", description_raw="t", description_norm="t",
            counterparty_norm="", dedupe_key=f"k{i}", seq=i,
        )
        for i, amount in enumerate(amounts)
    ]
    repository.insert_document(context, document, [], txns)


def _total(repository, context, direction):
    """The headline figure a client shows: every category row added up."""
    rows = repository.spending_summary(context, direction=direction)
    return sum(row["total_minor"] for row in rows)


class TestDirection:
    def test_each_side_is_kept_signed(self, repository, context):
        """Not absolute values. A household that spent more than it earned has
        to be able to see the minus, and an `in` total that arrived negative
        would read as a refund of everything."""
        _seed(repository, context, [-300_00, -200_00, 800_00])

        assert _total(repository, context, "out") == -500_00
        assert _total(repository, context, "in") == 800_00

    def test_net_is_the_two_sides_added(self, repository, context):
        """The number the household actually kept. If this ever stops equalling
        out + in, one of the three is filtering rows the others are not."""
        _seed(repository, context, [-300_00, -200_00, 800_00])

        out = _total(repository, context, "out")
        income = _total(repository, context, "in")
        assert _total(repository, context, "net") == out + income == 300_00

    def test_zero_belongs_to_neither_side(self, repository, context):
        """A zero-amount row is an artefact of a statement, not money moving.
        Counting it as spending would inflate the transaction count behind an
        unchanged total, which reads as a parser fault."""
        _seed(repository, context, [-100_00, 0, 100_00])

        assert _total(repository, context, "out") == -100_00
        assert _total(repository, context, "in") == 100_00

    def test_both_legs_of_a_transfer_leave_all_three(self, repository, context):
        """`net` is net of the outside world, not net of every row. Moving
        savings to a card is not income on one side and spending on the other,
        and a household that shuffled money between its own accounts would
        otherwise appear to have earned and spent it."""
        _seed(repository, context, [-100_00, 100_00])
        repository.mark_transfer(context, 1, 2)

        assert _total(repository, context, "out") == 0
        assert _total(repository, context, "in") == 0
        assert _total(repository, context, "net") == 0

    def test_a_one_sided_mark_leaves_only_its_own_side(self, repository, context):
        """Marking the outflow alone is a claim about that row, not about the
        one that happens to face it. A NULL counterpart must not be read as
        matching every other row — that emptied the whole dashboard once."""
        _seed(repository, context, [-100_00, 100_00])
        repository.mark_transfer(context, 1)

        assert _total(repository, context, "out") == 0
        assert _total(repository, context, "in") == 100_00
        assert _total(repository, context, "net") == 100_00

    def test_hiding_reaches_every_direction(self, repository, context):
        """Hiding that only worked on the spending screen would leave income
        and net describing a different set of rows from the one on screen."""
        _seed(repository, context, [-100_00, 250_00])
        repository.hide_txn(context, 2, note="")

        assert _total(repository, context, "in") == 0
        assert _total(repository, context, "net") == -100_00
        assert _total(repository, context, "out") == -100_00


class TestTrendCarriesBothSides:
    def test_a_bucket_reports_out_in_and_net(self, repository, context):
        _seed(repository, context, [-300_00, 800_00])

        points = repository.spending_trend(context, bucket="month")

        assert len(points) == 1
        point = points[0]
        assert point["out_minor"] == -300_00
        assert point["in_minor"] == 800_00
        assert point["net_minor"] == 500_00

    def test_total_minor_still_means_spending(self, repository, context):
        """Kept for clients written before income existed. Changing what it
        meant would have moved every existing dashboard's headline number
        without any of them being redeployed."""
        _seed(repository, context, [-300_00, 800_00])

        point = repository.spending_trend(context, bucket="month")[0]
        assert point["total_minor"] == point["out_minor"]


class TestRollingAverage:
    def _points(self, values):
        return _bucket(
            [(date(2026, 1 + i, 15), v) for i, v in enumerate(values)], "month",
        )

    def test_it_looks_backwards_only(self):
        """Trailing, not centred. A centred window needs periods that have not
        happened, so the newest bar — the one being asked about — would be the
        one the line could not describe."""
        points = _rolling(self._points([-100_00, -200_00, -900_00]), 3)

        # The middle point cannot see the 900 that follows it.
        assert points[1]["rolling"]["out_minor"] == -150_00
        assert points[2]["rolling"]["out_minor"] == -400_00

    def test_early_points_say_how_little_they_cover(self):
        """The first point's "3-month average" is one month. A client that drew
        it like the rest would show a trend that is only the series starting."""
        points = _rolling(self._points([-100_00, -200_00, -300_00, -400_00]), 3)

        assert [p["rolling_of"] for p in points] == [1, 2, 3, 3]

    def test_the_window_never_grows_past_itself(self):
        points = _rolling(self._points([-100_00] * 6), 3)
        assert max(p["rolling_of"] for p in points) == 3

    def test_all_three_measures_are_averaged(self):
        """A client switching the chart from spending to net must not fall back
        to an unaveraged line."""
        points = _rolling(self._points([-100_00, 300_00]), 2)
        assert set(points[0]["rolling"]) == {"out_minor", "in_minor", "net_minor"}

    def test_no_points_is_not_an_error(self):
        assert _rolling([], 3) == []

    @pytest.mark.parametrize("bucket,window", [("day", 7), ("week", 4), ("month", 3)])
    def test_the_default_window_spans_a_comparable_stretch(self, bucket, window):
        """Roughly a season at every bar width. A 3-bucket window on daily bars
        would track the weekend rather than the trend."""
        assert rolling_window(bucket) == window
