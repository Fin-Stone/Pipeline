"""Domain logic: money, dates, normalisation, dedupe."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.dates import DateParseError, parse_period, resolve_period_date
from app.domain.dedupe import assign_seq, dedupe_key
from app.domain.models import ParsedTxn
from app.domain.money import AmountParseError, from_minor, is_signed, parse_amount, to_minor
from app.domain.normalise import normalise_counterparty, normalise_description


class TestMoney:
    @pytest.mark.parametrize("text,minor,currency", [
        ("S$12,345.67", 1234567, "SGD"),
        ("+783.17", 78317, None),
        ("2,345.67", 234567, None),
        ("25.63 USD", 2563, "USD"),
        ("0.00", 0, None),
        ("(45.00)", -4500, None),
        ("-12.50", -1250, None),
        ("S$4,400.00", 440000, "SGD"),
        # The accounting trailing minus: a negative entry inside a column that
        # already has a direction, which is how DBS prints a reversal.
        ("100.00-", -10000, None),
        ("1,234.56-", -123456, None),
    ])
    def test_parses_statement_amounts(self, text, minor, currency):
        assert parse_amount(text) == (minor, currency)

    def test_round_trips_exactly(self):
        minor, _ = parse_amount("12,345.67")
        assert isinstance(minor, int) and minor == 1234567
        assert from_minor(minor, "SGD") == Decimal("12345.67")

    def test_summing_stays_exact_where_float_would_drift(self):
        # 0.1 + 0.2 != 0.3 in binary floating point. In minor units it is
        # simply 10 + 20 == 30, which is the entire reason for the choice.
        minors = [parse_amount(t)[0] for t in ("0.10", "0.20")]
        assert sum(minors) == parse_amount("0.30")[0]

    def test_rejects_sub_cent_precision(self):
        """Rounding is refused: an amount off by a fraction of a cent means the
        parse is wrong, and rounding it would corrupt reconciliation."""
        with pytest.raises(AmountParseError):
            to_minor(Decimal("1.005"), "SGD")

    def test_respects_zero_decimal_currencies(self):
        assert to_minor(Decimal("500"), "JPY") == 500
        assert to_minor(Decimal("5"), "SGD") == 500

    def test_rejects_nonsense(self):
        with pytest.raises(AmountParseError):
            parse_amount("Page 3 of 3")

    def test_written_sign_is_distinguishable_from_value(self):
        # Trust marks credits with "+" and leaves debits bare, so whether a
        # sign was written is information the magnitude cannot carry.
        assert is_signed("+783.17") is True
        assert is_signed("2,345.67") is False


class TestDates:
    def test_resolves_year_from_period(self):
        assert resolve_period_date("01 Jun", date(2023, 6, 1), date(2023, 6, 30)) == date(2023, 6, 1)

    def test_resolves_across_a_year_boundary(self):
        """The December/January case: the year comes from which end of the
        period the day actually falls in."""
        start, end = date(2024, 12, 15), date(2025, 1, 14)
        assert resolve_period_date("20 Dec", start, end) == date(2024, 12, 20)
        assert resolve_period_date("05 Jan", start, end) == date(2025, 1, 5)

    def test_raises_when_outside_the_period(self):
        with pytest.raises(DateParseError):
            resolve_period_date("15 Aug", date(2023, 6, 1), date(2023, 6, 30))

    def test_raises_when_ambiguous_rather_than_guessing(self):
        # A period spanning more than a year contains "05 Jan" twice.
        with pytest.raises(DateParseError, match="ambiguous"):
            resolve_period_date("05 Jan", date(2023, 1, 1), date(2024, 12, 31))

    def test_skips_impossible_dates(self):
        assert resolve_period_date("29 Feb", date(2023, 1, 1), date(2024, 3, 31)) == date(2024, 2, 29)

    def test_parses_period(self):
        assert parse_period("1 Jul 2025 - 31 Jul 2025") == (date(2025, 7, 1), date(2025, 7, 31))


class TestNormalise:
    def test_collapses_case_and_whitespace(self):
        assert normalise_description("  Pepper   Lunch ") == "PEPPER LUNCH"

    def test_strips_trailing_country_from_counterparty(self):
        assert normalise_counterparty("SNP*LITTLE ITALY SINGAPORE SG") == "SNP*LITTLE ITALY"

    def test_leaves_merchant_text_intact(self):
        assert normalise_counterparty("FairPrice App") == "FAIRPRICE APP"

    def test_drops_the_blank_reference_dbs_prints_after_a_paynow_payee(self):
        """DBS prints a reference and a purpose after the payee, writing `NA`
        where the payer left the reference empty. Whether those land on the
        payee's line or wrap onto the next is a fact about the name's length,
        so keeping `NA` made one merchant into two."""
        assert normalise_counterparty(
            "Advice FAST Payment / Receipt PAYNOW TRANSFER 1234567 TO: OLD CHANG GROUP PTE LTD NA OTHER"
        ) == normalise_counterparty(
            "Advice FAST Payment / Receipt PAYNOW TRANSFER 1234567 TO: OLD CHANG GROUP PTE LTD"
        ) == "OLD CHANG GROUP PTE LTD"

    def test_keeps_na_inside_a_name(self):
        """Trailing only. `NA` is a word merchants use, and editing the middle
        of a name is how one merchant becomes another."""
        assert normalise_counterparty("NA SIONG SEAFOOD") == "NA SIONG SEAFOOD"


class TestDedupe:
    def _txn(self, day, amount, description):
        return ParsedTxn(
            posted_date=date(2023, 6, day),
            amount_minor=amount,
            currency="SGD",
            description_raw=description,
        )

    def test_same_day_identical_amounts_get_distinct_seq(self):
        """Two $4.50 coffees on one day are two transactions, not a duplicate."""
        txns = [self._txn(12, -450, "Koufu"), self._txn(12, -450, "Koufu")]
        assert assign_seq(txns) == [0, 1]
        keys = {
            dedupe_key("acct", t.posted_date, t.amount_minor, "KOUFU", seq)
            for t, seq in zip(txns, assign_seq(txns))
        }
        assert len(keys) == 2

    def test_seq_is_per_group_not_global(self):
        txns = [self._txn(12, -450, "Koufu"), self._txn(12, -580, "Koufu"), self._txn(12, -450, "Koufu")]
        assert assign_seq(txns) == [0, 0, 1]

    def test_key_is_stable_across_runs(self):
        args = ("acct", date(2023, 6, 12), -450, "KOUFU", 0)
        assert dedupe_key(*args) == dedupe_key(*args)

    def test_key_changes_with_every_component(self):
        base = ("acct", date(2023, 6, 12), -450, "KOUFU", 0)
        variants = [
            ("other", date(2023, 6, 12), -450, "KOUFU", 0),
            ("acct", date(2023, 6, 13), -450, "KOUFU", 0),
            ("acct", date(2023, 6, 12), -451, "KOUFU", 0),
            ("acct", date(2023, 6, 12), -450, "OTHER", 0),
            ("acct", date(2023, 6, 12), -450, "KOUFU", 1),
        ]
        assert len({dedupe_key(*v) for v in [base, *variants]}) == 6


def test_amount_must_be_an_int_not_a_float():
    with pytest.raises(TypeError):
        ParsedTxn(posted_date=date(2023, 6, 1), amount_minor=4.5, currency="SGD", description_raw="x")
