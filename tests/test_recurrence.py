"""Finding the payments that repeat.

The cases that matter are the refusals. A list of subscriptions that quietly
includes things which are not subscriptions is worse than a shorter list, since
its whole purpose is to be glanced at and believed.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.domain.recurrence import (
    PERIOD_LABELS,
    Occurrence,
    declared_series,
    find_series,
    matching,
    price_rises,
)


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
        assert not find_series(_monthly("PAYLAH TOP UP", -10000))

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


class TestTwoPlansAtOnePrice:
    """One provider, two plans, the same price, billed a fortnight apart.

    Averaged together their gaps alternate 14 and 17 days, which lands on
    "fortnightly" — so two monthly plans were reported as one fortnightly one,
    at half the true commitment, and a price cut on one of them was invisible
    because the other went on at the old price.
    """

    #: The real dates, off the operator's statements. Two plans that drift
    #: independently, which is why their merged gaps alternate 14, 16, 17, 18
    #: and describe no period at all.
    DATES = (
        date(2025, 9, 30), date(2025, 10, 30), date(2025, 11, 15), date(2025, 11, 29),
        date(2025, 12, 16), date(2025, 12, 30), date(2026, 1, 16), date(2026, 1, 30),
        date(2026, 2, 17), date(2026, 3, 3), date(2026, 3, 19), date(2026, 4, 2),
    )

    def _two_plans(self, merchant, amount, months=6):
        return [
            Occurrence(txn_id=100 + i, merchant_norm=merchant, amount_minor=amount,
                       posted_date=day)
            for i, day in enumerate(self.DATES)
        ]

    def test_they_are_two_monthly_plans_not_one_fortnightly(self):
        series = find_series(self._two_plans("gomo", -1833))
        assert len(series) == 2
        assert all(s.period_label == "monthly" for s in series)

    def test_the_commitment_is_the_sum_of_both(self):
        series = find_series(self._two_plans("gomo", -1833))
        assert sum(s.monthly_equivalent_minor for s in series) == 1833 * 2

    def test_a_run_that_already_describes_something_is_left_alone(self):
        """The safeguard, and it is load-bearing. Anything threaded at three
        times its period also accounts for every row, in three tidy strands —
        so a rule that splits on "the strands fit better" splits everything. An
        irregular monthly premium became three quarterly ones this way."""
        rows = [
            Occurrence(txn_id=i, merchant_norm="premium", amount_minor=-80000,
                       posted_date=day)
            for i, day in enumerate([
                date(2025, 1, 15), date(2025, 2, 17), date(2025, 3, 14),
                date(2025, 4, 15), date(2025, 5, 19), date(2025, 6, 16),
                date(2025, 7, 15), date(2025, 8, 18), date(2025, 9, 15),
                date(2025, 10, 15), date(2025, 11, 17), date(2025, 12, 15),
            ])
        ]
        series = find_series(rows)
        assert len(series) == 1 and series[0].period_label == "monthly"

    def test_a_genuine_fortnightly_series_is_left_alone(self):
        """The guard on the guard. Anything threaded at three times its period
        also covers every row, so a rule loose enough to find two monthly plans
        would just as happily find three quarterly ones."""
        rows = [
            Occurrence(txn_id=i, merchant_norm="gym", amount_minor=-2500,
                       posted_date=date(2026, 1, 5) + timedelta(days=14 * i))
            for i in range(10)
        ]
        series = find_series(rows)
        assert len(series) == 1 and series[0].period_label == "fortnightly"

    def test_a_monthly_series_is_not_read_as_three_quarterly_ones(self):
        rows = _monthly("rent", -120000, months=12, start_year=2026) if False else [
            Occurrence(txn_id=i, merchant_norm="rent", amount_minor=-120000,
                       posted_date=date(2025, 1, 5) + timedelta(days=30 * i))
            for i in range(18)
        ]
        series = find_series(rows)
        assert len(series) == 1 and series[0].period_label == "monthly"


class TestAPriceChangeTooRecentToBeASeries:
    """A subscription whose price changed last month has two payments at the
    new price, and three are needed to establish one. Requiring three would
    mean a change is only visible a quarter after it happened — which is
    exactly when it stops being worth telling anybody."""

    def _cut(self, was, now, before=5, after=2):
        rows = [
            Occurrence(txn_id=i, merchant_norm="gomo", amount_minor=was,
                       posted_date=date(2026, 1, 5) + timedelta(days=30 * i))
            for i in range(before)
        ]
        rows += [
            Occurrence(txn_id=50 + i, merchant_norm="gomo", amount_minor=now,
                       posted_date=date(2026, 1, 5) + timedelta(days=30 * (before + i)))
            for i in range(after)
        ]
        return rows

    def test_the_cut_is_recorded_on_the_series(self):
        series = find_series(self._cut(-1833, -1333))
        assert len(series) == 1
        assert series[0].amount_centre_minor == 1333
        assert series[0].price_changes[-1].from_minor == 1833
        assert series[0].price_changes[-1].to_minor == 1333

    def test_one_payment_is_not_yet_a_change(self):
        """One is a coincidence with a plausible date; two show the cadence
        carried on."""
        series = find_series(self._cut(-1833, -1333, after=1))
        assert series[0].price_changes == ()

    def test_an_unrelated_large_payment_is_not_a_price_rise(self):
        """A tier change doubles a bill; it does not multiply it by eighty."""
        series = find_series(self._cut(-1000, -80000, after=2))
        assert all(not s.price_changes for s in series)

    def test_the_same_amount_again_is_not_a_change(self):
        """`400.00 -> 400.00` tells the operator nothing and looks like news."""
        series = find_series(self._cut(-40000, -40000))
        assert series[0].price_changes == ()


class TestPaymentsTheBankDidNotName:
    """A direct debit that reads only `GIRO PAYMENTS / COLLECTIONS VIA GIRO`
    names nobody, and no amount of normalising recovers a name that was never
    printed. Seven insurance premiums were invisible for this reason."""

    def _yearly(self, merchant, amount, years=3, txn_from=1):
        return [
            Occurrence(txn_id=txn_from + i, merchant_norm=merchant, amount_minor=amount,
                       posted_date=date(2023 + i, 6, 15))
            for i in range(years)
        ]

    def test_an_unnamed_direct_debit_is_still_found(self):
        series = find_series(self._yearly("GIRO PAYMENTS / COLLECTIONS VIA GIRO", -60000))
        assert len(series) == 1
        assert series[0].period_label == "yearly"
        assert series[0].merchant_norm == "Unnamed direct debit"

    def test_a_reference_stuck_to_the_rail_is_still_the_rail(self):
        """The same mechanism arrives bare and with the bank's own reference."""
        rows = self._yearly("GIRO PAYMENTS / COLLECTIONS VIA GIRO H123456789", -60000)
        assert len(find_series(rows)) == 1

    def test_the_named_half_and_the_unnamed_half_are_one_policy(self):
        """The case that was actually missed: the same premium filed under the
        insurer on two statements and under the rail on two others, so neither
        half reached the three occurrences a series needs."""
        rows = self._yearly("ACME LIFE", -60000, years=2, txn_from=1)
        rows += [
            Occurrence(txn_id=10 + i, merchant_norm="GIRO PAYMENTS / COLLECTIONS VIA GIRO",
                       amount_minor=-60000, posted_date=date(2025 + i, 6, 15))
            for i in range(2)
        ]
        series = find_series(rows)
        assert len(series) == 1
        assert series[0].occurrences == 4
        # Labelled with the name the bank did print, where it printed one.
        assert series[0].merchant_norm == "ACME LIFE"

    def test_a_named_payee_on_a_rail_prefix_is_not_grouped_by_amount(self):
        """`FAST ACME- INSURANCE PREMIUM` does name a payee."""
        from app.domain.recurrence import is_rail

        assert is_rail("FAST ACME- INSURANCE PREMIUM") is False
        assert is_rail("FAST") is True


class TestWhatAPersonKnowsThatTheRowsDoNot:
    """The detector's thresholds are right and still leave real commitments
    invisible: a yearly premium has two rows after two years, a plan taken out
    last month has one. Loosening the detector to reach those is what turned one
    monthly premium into three quarterly ones. The operator has the answer.
    """

    def _rows(self, days, amount=-24000, merchant="SOME INSURER"):
        return [
            Occurrence(txn_id=i, posted_date=day, amount_minor=amount, merchant_norm=merchant)
            for i, day in enumerate(days)
        ]

    def test_two_payments_are_a_series_when_declared(self):
        rows = self._rows([date(2024, 6, 15), date(2025, 6, 15)])
        assert find_series(rows) == []

        series = declared_series(rows, "SOME INSURER", 24000, "yearly")
        assert series.occurrences == 2
        assert series.period_label == "yearly"

    def test_one_payment_is_enough(self):
        """Nothing but a person can know a plan started last month, and
        refusing to record it is refusing the only source that knows."""
        series = declared_series(self._rows([date(2026, 7, 1)]), "SOME INSURER", 24000, "monthly")
        assert series.occurrences == 1
        assert series.expected_next == date(2026, 7, 31)

    def test_the_declared_period_is_not_second_guessed(self):
        """Two rows a year apart declared monthly stay monthly. Correcting the
        period to the one the dates imply would make this useless for the
        irregular billing it exists for."""
        rows = self._rows([date(2025, 1, 10), date(2026, 1, 10)])
        assert declared_series(rows, "SOME INSURER", 24000, "monthly").period_label == "monthly"

    def test_confidence_still_describes_the_gaps_and_nothing_else(self):
        """It means "how little the gaps varied". Two dates have one gap, which
        never varies, and reporting 1.0 would dress an assertion up as the
        strongest evidence available."""
        rows = self._rows([date(2024, 6, 15), date(2025, 6, 15)])
        assert declared_series(rows, "SOME INSURER", 24000, "yearly").confidence == 0.0

        # Exactly thirty days apart, because calendar months are 31 and 28 and
        # that variation is real: those gaps score 0.661, which is the number
        # doing its job rather than a problem with it.
        start = date(2026, 1, 10)
        steady = self._rows([start + timedelta(days=30 * i) for i in range(3)])
        assert declared_series(steady, "SOME INSURER", 24000, "monthly").confidence == 1.0

    def test_it_gathers_the_merchant_not_the_one_charge(self):
        """The mark is on a merchant, and this is the difference that has to be
        visible before it is written rather than found in a monthly total."""
        rows = self._rows([date(2024, 6, 15), date(2025, 6, 15), date(2026, 6, 15)])
        rows += self._rows([date(2025, 8, 1)], amount=-1200, merchant="SOMEWHERE ELSE")
        assert len(matching(rows, "SOME INSURER", 24000)) == 3

    def test_a_price_that_crept_is_still_the_same_commitment(self):
        """The ordinary amount tolerance, so a premium that rose with tax does
        not fall out of the series a person marked."""
        rows = self._rows([date(2024, 6, 15)]) + self._rows([date(2025, 6, 15)], amount=-25000)
        assert len(matching(rows, "SOME INSURER", 24000)) == 2

    def test_an_unrelated_amount_is_not_swept_in(self):
        rows = self._rows([date(2024, 6, 15)]) + self._rows([date(2025, 6, 15)], amount=-90000)
        assert len(matching(rows, "SOME INSURER", 24000)) == 1

    def test_a_merchant_with_no_rows_left_declares_nothing(self):
        """A reparse can rename a merchant. Returning a series over no rows
        would put a commitment in the monthly total with nothing behind it."""
        assert declared_series([], "GONE", 1000, "monthly") is None

    def test_every_period_it_offers_is_one_it_can_express(self):
        """`PERIOD_LABELS` is what a client puts in front of somebody. A label
        the rest of the module cannot express would be a dropdown entry that
        fails on submit."""
        rows = self._rows([date(2026, 1, 10), date(2026, 2, 10)])
        for label in PERIOD_LABELS:
            assert declared_series(rows, "SOME INSURER", 24000, label).period_label == label
