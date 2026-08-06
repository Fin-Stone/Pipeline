"""Money that came back for one specific charge.

The case: one person pays $300 for a group dinner and four others send back $60
each. Recorded faithfully, the ledger says $300 of Dining and $240 of income
from nowhere. Both are false — the household spent $60 and earned nothing.

A transfer link cannot say this. It excludes both legs, which is right for money
moving between one's own accounts and wrong here, because $60 really was spent.

The sharpest check in this file is that **net does not move**. A payback shifts
money between two figures without creating or destroying any, so if `out + in`
changes when a link is made, the arithmetic is wrong somewhere.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.domain.models import DEPOSIT
from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

SHA = "a" * 64


def _seed(repository, context, rows, *, sha=SHA, month=6):
    """`rows` are (day, amount_minor, counterparty). Ids come back in order.

    A second call needs its own `month` as well as its own `sha`: a statement is
    identified by its accounts and period, not by its bytes, so re-using the
    period would be claiming this is the same statement arriving twice.
    """
    account = AccountRecord(
        institution="Test", account_ref_masked="1234", sub_account_label="",
        currency="SGD", kind=DEPOSIT,
    )
    repository.insert_document(
        context,
        DocumentRecord(
            sha256=sha, institution="Test", doc_type="acc",
            period_start=date(2026, month, 1), period_end=date(2026, month, 28),
            storage_path="x", parse_status="imported",
            source_profile="dummy", source_relpath="a.pdf",
            fetched_at=datetime.now(timezone.utc),
        ),
        [],
        [
            TxnRecord(
                account_key=account, posted_date=date(2026, month, day), amount_minor=amount,
                currency="SGD", description_raw=name, description_norm=name.upper(),
                counterparty_norm=name.upper(), dedupe_key=f"{sha[:4]}-{i}", seq=i,
            )
            for i, (day, amount, name) in enumerate(rows)
        ],
    )


def _totals(repository, context):
    """The three headline figures, as the dashboard computes them."""
    return {
        d: sum(r["total_minor"] or 0 for r in repository.spending_summary(context, direction=d))
        for d in ("out", "in", "net")
    }


@pytest.fixture
def dinner(repository, context):
    """A $300 charge on the 3rd and four $60 inflows over the following week."""
    _seed(repository, context, [
        (3, -300_00, "DIN TAI FUNG"),
        (4, 60_00, "PAYNOW A"),
        (5, 60_00, "PAYNOW B"),
        (6, 60_00, "PAYNOW C"),
        (7, 60_00, "PAYNOW D"),
    ])
    return {"charge": 1, "paybacks": [2, 3, 4, 5]}


class TestItReducesTheChargeRatherThanRemovingIt:
    def test_the_charge_shrinks_by_what_came_back(self, repository, context, dinner):
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        assert _totals(repository, context)["out"] == -60_00

    def test_the_paybacks_stop_being_income(self, repository, context, dinner):
        """Otherwise the household appears to have earned $240 from nowhere."""
        assert _totals(repository, context)["in"] == 240_00
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        assert _totals(repository, context)["in"] == 0

    def test_net_does_not_move(self, repository, context, dinner):
        """The whole claim in one assertion. A payback relabels money; it does
        not create or destroy any, so the household's position is the same
        before and after somebody presses the button."""
        before = _totals(repository, context)["net"]
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        assert _totals(repository, context)["net"] == before == -60_00

    def test_a_partly_settled_charge_keeps_the_rest(self, repository, context, dinner):
        """Two of four have paid. The household is out $180 so far, and the two
        who have not paid are not income that never arrived."""
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"][:2])
        totals = _totals(repository, context)
        assert totals["out"] == -180_00
        assert totals["in"] == 120_00
        assert totals["net"] == -60_00

    def test_the_trend_agrees_with_the_totals(self, repository, context, dinner):
        """The chart and the tiles are on the same screen. One reducing the
        charge while the other did not would be visible and unexplainable."""
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        points = repository.spending_trend(context, bucket="month")
        assert len(points) == 1
        assert points[0]["out_minor"] == -60_00
        assert points[0]["in_minor"] == 0
        assert points[0]["net_minor"] == -60_00

    def test_the_row_carries_both_figures(self, repository, context, dinner):
        """A client must be able to show -300.00 -> -60.00. Deriving the second
        itself would be arithmetic on money this API promised to do."""
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        row = next(
            r for r in repository.list_spending(context) if r["id"] == dinner["charge"]
        )
        assert row["amount_minor"] == -300_00
        assert row["paid_back_minor"] == 240_00
        assert row["effective_amount_minor"] == -60_00

    def test_an_untouched_row_reports_nothing_paid_back(self, repository, context, dinner):
        """The outer join gives NULL for almost every row in a real ledger."""
        _seed(repository, context, [(9, -20_00, "COFFEE")], sha="b" * 64, month=7)
        row = next(r for r in repository.list_spending(context) if r["counterparty_norm"] == "COFFEE")
        assert row["paid_back_minor"] == 0
        assert row["effective_amount_minor"] == -20_00

    def test_unlinking_restores_both_sides(self, repository, context, dinner):
        before = _totals(repository, context)
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        assert repository.unlink_paybacks(context, dinner["charge"]) == 4
        assert _totals(repository, context) == before

    def test_one_payback_can_be_undone_alone(self, repository, context, dinner):
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        assert repository.unlink_paybacks(context, dinner["charge"], dinner["paybacks"][0]) == 1
        assert _totals(repository, context)["out"] == -120_00


class TestItRefusesRatherThanInventing:
    def test_more_back_than_was_spent(self, repository, context):
        """A charge cannot become income. Somebody has linked the wrong row,
        and quietly clamping it would leave a number nobody can explain."""
        _seed(repository, context, [(3, -50_00, "LUNCH"), (4, 80_00, "PAYNOW")])
        with pytest.raises(ValueError, match="more than the charge"):
            repository.link_paybacks(context, 1, [2])

    def test_the_refusal_names_both_numbers(self, repository, context):
        _seed(repository, context, [(3, -50_00, "LUNCH"), (4, 80_00, "PAYNOW")])
        with pytest.raises(ValueError, match=r"80\.00.*50\.00"):
            repository.link_paybacks(context, 1, [2])

    def _extra(self, repository, context, name, sha, month):
        _seed(repository, context, [(8, 60_00, name)], sha=sha * 64, month=month)
        return next(
            r["id"] for r in repository.list_spending(context, direction="in")
            if r["counterparty_norm"] == name
        )

    def test_being_paid_back_in_full_is_allowed(self, repository, context, dinner):
        """You fronted $300 for five other people and were not eating. The
        charge nets to nothing, which is the truth and not an error."""
        fifth = self._extra(repository, context, "PAYNOW E", "c", 7)
        repository.link_paybacks(context, dinner["charge"], [*dinner["paybacks"], fifth])
        assert _totals(repository, context)["out"] == 0

    def test_it_counts_what_is_already_linked(self, repository, context, dinner):
        """The ceiling is the whole charge, not each link. Five $60 paybacks
        fit a $300 charge exactly; a sixth is somebody linking the wrong row,
        even though on its own it would have been fine."""
        fifth = self._extra(repository, context, "PAYNOW E", "c", 7)
        sixth = self._extra(repository, context, "PAYNOW F", "d", 8)
        repository.link_paybacks(context, dinner["charge"], [*dinner["paybacks"], fifth])

        with pytest.raises(ValueError, match="more than the charge"):
            repository.link_paybacks(context, dinner["charge"], [sixth])

    def test_an_inflow_cannot_settle_two_charges(self, repository, context):
        """One person's $60 discounting two dinners would take $120 off the
        household's spending on the strength of $60."""
        _seed(repository, context, [
            (3, -300_00, "DINNER ONE"), (4, -300_00, "DINNER TWO"), (5, 60_00, "PAYNOW"),
        ])
        repository.link_paybacks(context, 1, [3])
        with pytest.raises(ValueError, match="already settles charge 1"):
            repository.link_paybacks(context, 2, [3])

    def test_the_charge_must_be_money_out(self, repository, context, dinner):
        with pytest.raises(ValueError, match="not a charge"):
            repository.link_paybacks(context, dinner["paybacks"][0], [dinner["charge"]])

    def test_a_payback_must_be_money_in(self, repository, context):
        _seed(repository, context, [(3, -300_00, "DINNER"), (4, -60_00, "ALSO SPENDING")])
        with pytest.raises(ValueError, match="not money in"):
            repository.link_paybacks(context, 1, [2])

    def test_a_transfer_leg_is_not_a_payback(self, repository, context, dinner):
        """Money the household moved to itself was never anyone's repayment,
        and counting it as one would discount a real charge with its own money.
        """
        repository.mark_transfer(context, dinner["paybacks"][0])
        with pytest.raises(ValueError, match="already a transfer or hidden"):
            repository.link_paybacks(context, dinner["charge"], [dinner["paybacks"][0]])

    def test_a_hidden_row_is_not_a_payback(self, repository, context, dinner):
        repository.hide_txn(context, dinner["paybacks"][0], note="")
        with pytest.raises(ValueError, match="already a transfer or hidden"):
            repository.link_paybacks(context, dinner["charge"], [dinner["paybacks"][0]])

    def test_a_transaction_from_nowhere(self, repository, context, dinner):
        with pytest.raises(LookupError):
            repository.link_paybacks(context, dinner["charge"], [9999])

    def test_nothing_lands_when_one_row_is_refused(self, repository, context, dinner):
        """Linking five people is one action. A partial success would leave the
        charge discounted by an amount the operator never chose."""
        with pytest.raises(LookupError):
            repository.link_paybacks(context, dinner["charge"], [dinner["paybacks"][0], 9999])
        assert repository.list_paybacks(context, dinner["charge"]) == []

    def test_the_same_row_twice_in_one_call_counts_once(self, repository, context, dinner):
        result = repository.link_paybacks(
            context, dinner["charge"], [dinner["paybacks"][0], dinner["paybacks"][0]]
        )
        assert result["linked"] == 1
        assert _totals(repository, context)["out"] == -240_00


class TestCandidates:
    def test_it_offers_the_inflows_nearest_the_charge(self, repository, context, dinner):
        found = repository.list_payback_candidates(context, dinner["charge"])
        assert [r["id"] for r in found] == dinner["paybacks"]

    def test_it_does_not_offer_what_is_already_taken(self, repository, context, dinner):
        """Offering a choice that would be refused is worse than not offering."""
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"][:2])
        found = repository.list_payback_candidates(context, dinner["charge"])
        assert [r["id"] for r in found] == dinner["paybacks"][2:]

    def test_it_does_not_offer_spending(self, repository, context, dinner):
        assert all(r["amount_minor"] > 0 for r in
                   repository.list_payback_candidates(context, dinner["charge"]))

    def test_a_window_narrows_it(self, repository, context, dinner):
        found = repository.list_payback_candidates(context, dinner["charge"], within_days=2)
        assert [r["id"] for r in found] == dinner["paybacks"][:2]

    def test_it_can_be_searched(self, repository, context, dinner):
        found = repository.list_payback_candidates(context, dinner["charge"], q="paynow c")
        assert [r["counterparty_norm"] for r in found] == ["PAYNOW C"]

    def test_an_unknown_charge_is_a_lookup_error(self, repository, context):
        with pytest.raises(LookupError):
            repository.list_payback_candidates(context, 9999)


class TestItDoesNotChangeWhatSomethingWas:
    """A $300 dinner is a Dining charge whoever ended up paying for it.

    The adjustment belongs to figures that *total* money. Categorisation asks
    what a transaction was, and the answer does not depend on who reimbursed it.
    """

    def test_categorisation_still_sees_the_whole_charge(self, repository, context, dinner):
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        target = next(
            t for t in repository.list_categorisation_targets(context)
            if t["id"] == dinner["charge"]
        )
        assert target["amount_minor"] == -300_00

    def test_the_charge_is_still_there_to_categorise(self, repository, context, dinner):
        """Unlike a transfer, which drops out of categorisation entirely."""
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        assert dinner["charge"] in {t["id"] for t in repository.list_categorisation_targets(context)}


class TestMoneyStaysAnInteger:
    """Every `_minor` field is a whole number of cents on both engines.

    Postgres widens `SUM(bigint)` to `numeric` so it cannot overflow, and that
    arrives as a Decimal and serialises to a quoted JSON string. Adding it to
    an amount spreads the problem to the amount. This is the second time that
    has bitten, so it is asserted rather than hoped for.
    """

    def test_the_repository_returns_integers(self, repository, context, dinner):
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        row = next(r for r in repository.list_spending(context) if r["id"] == dinner["charge"])
        assert isinstance(row["paid_back_minor"], int)
        assert isinstance(row["effective_amount_minor"], int)

    def test_a_trend_bucket_returns_integers(self, repository, context, dinner):
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        point = repository.spending_trend(context, bucket="month")[0]
        for field in ("out_minor", "in_minor", "net_minor"):
            assert isinstance(point[field], int), field


class TestItSurvives:
    def test_a_backup_carries_the_links(self):
        """Paybacks are operator decisions and nothing regenerates them, so a
        backup that skipped them would restore a ledger quietly overstating its
        own spending. `archive.create` walks `metadata.sorted_tables`, so being
        in there is the whole mechanism — and forgetting to add a table to the
        metadata is the way this would actually break.
        """
        from app.storage import schema

        assert schema.payback_link in schema.metadata.sorted_tables
        assert schema.payback_link in schema.TENANT_SCOPED_TABLES

    def test_deleting_the_document_takes_the_links_with_it(self, repository, context, dinner):
        """A link pointing at a row that is about to go would block the delete
        on a foreign key — which is exactly how reparse broke on Postgres."""
        repository.link_paybacks(context, dinner["charge"], dinner["paybacks"])
        assert repository.delete_document(context, SHA) is True
        assert repository.list_paybacks(context) == []

    def test_a_reparse_survives_categorised_and_hidden_rows(self, repository, context, dinner):
        """`delete_document` cleared only `transfer_link`, and SQLite does not
        enforce foreign keys unless asked to — so nothing noticed that
        `txn_enrichment`, `hidden_txn` and `txn_series_link` all point at rows
        it deletes. Postgres always enforces them, and `finstone reparse` on any
        categorised document died on a constraint violation. Prod held nearly
        two thousand enrichment rows by then.
        """
        repository.set_human_category(context, dinner["charge"], "Dining")
        repository.hide_txn(context, dinner["paybacks"][-1], note="")
        repository.mark_transfer(context, dinner["paybacks"][0])

        assert repository.delete_document(context, SHA) is True
        assert repository.counts(context).txns == 0
