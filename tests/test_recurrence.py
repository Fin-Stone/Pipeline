"""Finding the payments that repeat.

The cases that matter are the refusals. A list of subscriptions that quietly
includes things which are not subscriptions is worse than a shorter list, since
its whole purpose is to be glanced at and believed.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.domain.recurrence import Occurrence, Series, find_series, price_rises


def _monthly(merchant, amount, months=6, day=15, start_year=2026, txn_from=1):
    """One payment a month, as a direct debit actually arrives."""
    rows = []
    for index in range(months):
        month = 1 + index
        rows.append(Occurrence(
            txn_id=txn_from + index,
            posted_date=date(start_year, month, day),
            amount_minor=amount,
            merchant_norm=merchant,
        ))
    return rows


class TestFindingSeries:
    def test_a_monthly_subscription_is_found(self):
        series = find_series(_monthly("netflix", -1999))
        assert len(series) == 1
        assert series[0].period_label == "monthly"
        assert series[0].amount_centre_minor == 1999
        assert series[0].occurrences == 6

    def test_the_next_payment_is_predicted(self):
        series = find_series(_monthly("netflix", -1999, months=4))[0]
        assert series.last_seen == date(2026, 4, 15)
        assert series.expected_next == date(2026, 4, 15) + timedelta(days=30)

    def test_month_end_drift_is_still_monthly(self):
        """A debit on the 31st has gaps of 28 to 31 and is not irregular."""
        rows = [
            Occurrence(1, date(2026, 1, 31), -5000, "gym"),
            Occurrence(2, date(2026, 2, 28), -5000, "gym"),
            Occurrence(3, date(2026, 3, 31), -5000, "gym"),
            Occurrence(4, date(2026, 4, 30), -5000, "gym"),
        ]
        assert find_series(rows)[0].period_label == "monthly"

    def test_a_creeping_price_stays_one_series(self):
        """Subscriptions creep with tax and tier changes without becoming a
        different thing."""
        rows = _monthly("spotify", -1000)
        rows[-1] = Occurrence(rows[-1].txn_id, rows[-1].posted_date, -1050, "spotify")
        series = find_series(rows)
        assert len(series) == 1 and series[0].occurrences == 6

    def test_two_subscriptions_to_one_shop_stay_apart(self):
        """Averaging them together would describe neither."""
        rows = _monthly("apple", -299) + _monthly("apple", -1499, txn_from=100)
        found = find_series(rows)
        assert len(found) == 2
        assert {s.amount_centre_minor for s in found} == {299, 1499}

    def test_yearly_is_recognised(self):
        rows = [
            Occurrence(i, date(2023 + i, 3, 4), -12000, "insurance")
            for i in range(3)
        ]
        assert find_series(rows)[0].period_label == "yearly"


class TestRefusals:
    def test_two_occurrences_are_not_a_series(self):
        """Any two dates have an interval, and it is always perfectly
        regular."""
        assert not find_series(_monthly("netflix", -1999, months=2))

    def test_irregular_spending_is_not_a_series(self):
        rows = [
            Occurrence(1, date(2026, 1, 3), -2000, "cafe"),
            Occurrence(2, date(2026, 1, 20), -2000, "cafe"),
            Occurrence(3, date(2026, 3, 2), -2000, "cafe"),
            Occurrence(4, date(2026, 3, 4), -2000, "cafe"),
        ]
        assert not find_series(rows)

    def test_a_regular_gap_with_no_human_name_is_not_reported(self):
        """Something arriving every 47 days reliably is real, but calling it a
        subscription would be the model talking rather than the data."""
        rows = [
            Occurrence(i, date(2026, 1, 1) + timedelta(days=47 * i), -2000, "odd")
            for i in range(4)
        ]
        assert not find_series(rows)

    def test_repeats_on_one_day_are_not_a_period(self):
        rows = [Occurrence(i, date(2026, 1, 5), -450, "coffee") for i in range(4)]
        assert not find_series(rows)

    def test_a_different_amount_starts_a_different_series(self):
        """One large payment among small ones is not part of their series."""
        rows = _monthly("shop", -1000, months=4)
        rows.append(Occurrence(99, date(2026, 5, 15), -80000, "shop"))
        found = find_series(rows)
        assert len(found) == 1 and found[0].amount_centre_minor == 1000


class TestPriceChanges:
    """A price rise is a fact about a subscription, not a new subscription."""

    def _risen(self):
        rows = [Occurrence(i, date(2025, 1 + i, 15), -999, "netflix") for i in range(8)]
        for i in range(8):
            month, year = 9 + i, 2025
            if month > 12:
                month, year = month - 12, 2026
            rows.append(Occurrence(100 + i, date(year, month, 15), -1299, "netflix"))
        return rows

    def test_a_rise_past_tolerance_stays_one_subscription(self):
        """Clustering splits at 7%, and 9.99 to 12.99 is 30%. Left split, the
        overview double-counts, the list shows the service twice, and the
        abandoned half looks permanently overdue."""
        series = find_series(self._risen())
        assert len(series) == 1
        assert series[0].occurrences == 16

    def test_the_rise_itself_is_recorded(self):
        """Which is the whole of what "trend subscription pricing" needs."""
        change = find_series(self._risen())[0].price_changes
        assert len(change) == 1
        assert (change[0].from_minor, change[0].to_minor) == (999, 1299)
        assert change[0].on == date(2025, 9, 15)

    def test_the_centre_is_what_it_costs_now(self):
        series = find_series(self._risen())[0]
        assert series.amount_centre_minor == 1299

    def test_the_total_is_what_was_actually_paid(self):
        """Summed from the occurrences, not from the centre, so a series that
        changed price still totals truthfully."""
        assert find_series(self._risen())[0].total_paid_minor == 8 * 999 + 8 * 1299

    def test_a_service_taken_up_again_years_later_is_not_one_series(self):
        """A gap of years at a different price is a new subscription, not a
        price change. Merging them would invent a single commitment that was
        never held continuously."""
        rows = _monthly("gym", -5000, months=4)
        rows += [
            Occurrence(200 + i, date(2029, 1 + i, 15), -9000, "gym")
            for i in range(4)
        ]
        found = find_series(rows)
        assert len(found) == 2
        assert all(not s.price_changes for s in found)


class TestServingTheFourPurposes:
    def test_a_yearly_and_a_monthly_compare_on_the_same_footing(self):
        """A yearly 120 and a monthly 10 are the same commitment."""
        yearly = find_series([
            Occurrence(i, date(2023 + i, 3, 4), -12000, "insurance") for i in range(3)
        ])[0]
        monthly = find_series(_monthly("streaming", -1000, months=4))[0]
        assert yearly.monthly_equivalent_minor == monthly.monthly_equivalent_minor

    def test_upcoming_payments_can_be_asked_for(self):
        series = find_series(_monthly("netflix", -1999, months=4))[0]
        assert series.is_due_within(series.expected_next - timedelta(days=3), 7)
        assert not series.is_due_within(series.expected_next - timedelta(days=30), 7)

    def test_lapsed_is_not_missed(self):
        """A cancelled subscription and a skipped payment want opposite
        reactions, and only one of them should nag."""
        series = find_series(_monthly("gone", -500, months=4))[0]
        missed = series.expected_next + timedelta(days=10)
        long_gone = series.expected_next + timedelta(days=400)

        assert series.is_overdue(missed) and not series.is_lapsed(missed)
        assert series.is_lapsed(long_gone) and not series.is_overdue(long_gone)


class TestConduits:
    """A wallet top-up is spending, but it is not a subscription."""

    def test_an_ewallet_topped_up_monthly_is_not_a_subscription(self):
        """It is the most regular thing in a ledger, so it would be found
        early and believed, while telling the operator nothing they can
        cancel."""
        assert not find_series(_monthly("PAYLAH EXAMPLE HOLDER", -10000))

    def test_a_marketplace_is_not_a_subscription(self):
        assert not find_series(_monthly("LAZADA SINGAPORE PAYM", -5000))

    def test_a_service_bought_through_one_still_counts(self):
        """A real subscription arrives under its own name, so nothing genuine
        is lost by excluding the route it was paid over."""
        assert len(find_series(_monthly("NETFLIX", -1999))) == 1

    def test_cash_is_not_a_merchant(self):
        assert not find_series(_monthly("CASH WITHDRAWAL TOH GUAN", -20000))


class TestAlerts:
    def test_a_missed_payment_is_overdue(self):
        series = find_series(_monthly("netflix", -1999, months=4))[0]
        assert not series.is_overdue(series.expected_next)
        # Inside the period's own slack, a late debit is not a missed one.
        assert not series.is_overdue(series.expected_next + timedelta(days=2))
        assert series.is_overdue(series.expected_next + timedelta(days=10))

    def test_a_price_rise_is_surfaced(self):
        """Still paid, still on time, and otherwise invisible."""
        series = find_series(_monthly("news", -1000, months=4))[0]
        risen = [Occurrence(50, date(2026, 5, 15), -1400, "news")]
        assert price_rises(series, risen) == risen

    def test_an_ordinary_variation_is_not_a_price_rise(self):
        series = find_series(_monthly("news", -1000, months=4))[0]
        assert not price_rises(series, [Occurrence(50, date(2026, 5, 15), -1030, "news")])


class TestConfidence:
    def test_perfectly_regular_reads_as_certain(self):
        rows = [
            Occurrence(i, date(2026, 1, 1) + timedelta(days=7 * i), -500, "bus")
            for i in range(5)
        ]
        found = find_series(rows)[0]
        assert found.period_label == "weekly" and found.confidence == 1.0

    def test_a_looser_series_reads_as_less_certain(self):
        rows = [
            Occurrence(1, date(2026, 1, 1), -500, "x"),
            Occurrence(2, date(2026, 1, 9), -500, "x"),
            Occurrence(3, date(2026, 1, 15), -500, "x"),
            Occurrence(4, date(2026, 1, 23), -500, "x"),
        ]
        found = find_series(rows)
        assert found and 0.0 < found[0].confidence < 1.0
