"""Adapter tests against the operator's real documents.

uploads/dummy is gitignored, so these skip cleanly when the documents are
absent — CI stays green without operator data. When they are present, these are
the tests that actually prove the adapters read real statements correctly.

Values asserted here were read directly out of the PDFs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.parsers import fingerprint as fingerprinting
from app.parsers import pdfio
from app.parsers.registry import build_default_registry
from app.parsers.trust.acc import TrustAccountAdapter
from app.parsers.trust.cc import PRODUCT, TrustCardAdapter

pytestmark = pytest.mark.requires_dummy

TRUST_ACC_2025 = "Trust Bank/acc/2025 July Statement_1000000000000000001.pdf"
TRUST_ACC_2024 = "Trust Bank/acc/2024 June Statement_1000000000000000002.pdf"
TRUST_CC_2023 = "Trust Bank/cc/2023 June Statement_1000000000000000003.pdf"


def _require(dummy_root, relpath):
    path = dummy_root / relpath
    if not path.exists():
        pytest.skip(f"{relpath} not present")
    return path


class TestTrustSavings:
    def test_parses_three_pockets_each_reconciling(self, dummy_root):
        parsed = TrustAccountAdapter().parse(_require(dummy_root, TRUST_ACC_2025))

        assert parsed.institution == "Trust Bank"
        assert parsed.period_start == date(2025, 7, 1)
        assert parsed.period_end == date(2025, 7, 31)
        assert parsed.statement_date == date(2025, 8, 3)

        pockets = {a.sub_account_label: a for a in parsed.accounts}
        assert set(pockets) == {"Main Account", "Test", "Huat Ah"}
        assert all(a.account_ref_masked == "01-2345678-9" for a in parsed.accounts)

        main = pockets["Main Account"]
        assert main.opening_balance_minor == 10000000
        assert main.closing_balance_minor == 10000000
        assert main.opening_balance_minor + sum(t.amount_minor for t in main.txns) == main.closing_balance_minor

    def test_credits_are_positive_and_debits_negative(self, dummy_root):
        parsed = TrustAccountAdapter().parse(_require(dummy_root, TRUST_ACC_2025))
        main = next(a for a in parsed.accounts if a.sub_account_label == "Main Account")
        amounts = {t.description_raw: t.amount_minor for t in main.txns}
        assert amounts["FPG"] == 78317                    # "+783.17"
        assert amounts["Credit card payment"] == -117903  # "1,000.00"
        assert amounts["Interest"] == 15345               # "+153.45"

    def test_single_pocket_statement_also_parses(self, dummy_root):
        parsed = TrustAccountAdapter().parse(_require(dummy_root, TRUST_ACC_2024))
        assert len(parsed.accounts) == 1
        account = parsed.accounts[0]
        assert account.closing_balance_minor == 400000
        assert account.opening_balance_minor + sum(t.amount_minor for t in account.txns) == 400000

    def test_both_years_route_to_the_same_adapter(self, dummy_root):
        """Layout identity must survive a change in pocket count (1 vs 3)."""
        registry = build_default_registry()
        for relpath in (TRUST_ACC_2024, TRUST_ACC_2025):
            document = pdfio.load(_require(dummy_root, relpath))
            assert registry.resolve(document).name == "trust.acc"

    def test_routing_does_not_depend_on_customer_data(self, dummy_root):
        """The signature must name only the bank's own words. Requiring a name
        or an address would mean moving house broke the adapter — and would put
        personal data in the routing table."""
        from app.parsers.trust.acc import SIGNATURE

        document = pdfio.load(_require(dummy_root, TRUST_ACC_2025))
        labels = set(fingerprinting.label_lines(document.pages[0]))
        customer_lines = labels - set(SIGNATURE.requires)

        assert SIGNATURE.matches(document)
        # There is customer text in the header band, and none of it is required.
        assert customer_lines
        assert not any("example" in line for line in SIGNATURE.requires)


class TestTrustCard:
    def test_reconciles_with_the_deposit_formula(self, dummy_root):
        """Amounts owed are stored negated, so one formula covers both
        statement types: 4.24 owed + 2.35 net spend = 6.59 owed."""
        parsed = TrustCardAdapter().parse(_require(dummy_root, TRUST_CC_2023))
        account = parsed.accounts[0]

        assert account.kind == "card"
        assert account.opening_balance_minor == -424
        assert account.closing_balance_minor == -659
        assert account.opening_balance_minor + sum(t.amount_minor for t in account.txns) == -659

    def test_identified_by_product_not_card_number(self, dummy_root):
        """Card numbers change on reissue while the account continues, so the
        product is the stable identity."""
        parsed = TrustCardAdapter().parse(_require(dummy_root, TRUST_CC_2023))
        assert parsed.accounts[0].account_ref_masked == PRODUCT

    def test_purchases_negative_payments_positive(self, dummy_root):
        parsed = TrustCardAdapter().parse(_require(dummy_root, TRUST_CC_2023))
        txns = parsed.accounts[0].txns

        purchases = [t.amount_minor for t in txns if t.description_raw == "Pepper Lunch"]
        assert purchases == [-3090]

        # The statement carries two payments, on 10 and 24 June. Both are
        # credits and both must survive as separate rows.
        payments = sorted(t.amount_minor for t in txns if t.description_raw == "FAST Credit Payment")
        assert payments == [20900, 35000]

    def test_foreign_currency_row_keeps_both_amounts(self, dummy_root):
        """The multi-line FCY row: merchant above, date and amounts on the
        row itself, exchange rate below."""
        parsed = TrustCardAdapter().parse(_require(dummy_root, TRUST_CC_2023))
        fx = [t for t in parsed.accounts[0].txns if t.fx_amount_minor is not None]
        assert len(fx) == 1
        row = fx[0]
        assert row.posted_date == date(2023, 6, 21)
        assert row.amount_minor == -3433        # settled SGD, what reconciles
        assert row.fx_amount_minor == 2563      # 25.63 as originally billed
        assert row.fx_currency == "USD"
        assert row.fx_rate == Decimal("1.3394")
        assert "indianvisaonline" in row.description_raw


DBS_ACC = "DBS/acc/-2.pdf"


class TestDbsSavings:
    """DBS differs from Trust in every way that matters to a table reader:
    two amount columns, right-aligned values, a running balance, numeric
    dates, and references trailing under each row."""

    def test_reconciles(self, dummy_root):
        from app.parsers.dbs.acc import DbsAccountAdapter

        parsed = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC))
        assert parsed.institution == "DBS"
        assert len(parsed.accounts) == 1

        account = parsed.accounts[0]
        assert account.account_ref_masked == "96-5432109-2"
        assert account.opening_balance_minor == 5000000
        assert account.closing_balance_minor == 5000000
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_the_column_decides_the_direction(self, dummy_root):
        """DBS never signs an amount; withdrawals and deposits are separate
        columns."""
        from app.parsers.dbs.acc import DbsAccountAdapter

        account = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC)).accounts[0]
        amounts = {t.description_raw.split()[0]: t.amount_minor for t in account.txns}
        assert account.txns[0].amount_minor == -54180        # Withdrawal column
        assert any(t.amount_minor == 4700 for t in account.txns)   # Deposit column
        assert "Interest" in amounts

    def test_period_is_inferred_from_the_as_at_date(self, dummy_root):
        """DBS prints no period, only "as at 31 Dec 2021"."""
        from datetime import date as _date
        from app.parsers.dbs.acc import DbsAccountAdapter

        parsed = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC))
        assert parsed.period_start == _date(2021, 12, 1)
        assert parsed.period_end == _date(2021, 12, 31)

    def test_pages_of_one_account_are_merged(self, dummy_root):
        """The Account No. header repeats per page. Treating each page as its
        own account splits the month and each fragment then disagrees with the
        totals the statement declares."""
        from app.parsers.dbs.acc import DbsAccountAdapter

        parsed = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC))
        assert len(parsed.accounts) == 1
        assert len(parsed.accounts[0].txns) == 20

    def test_trailing_references_stay_on_their_own_row(self, dummy_root):
        """The last reference sits 42pt under its row against a 48pt pitch;
        a fixed gap leaked it onto the following transaction."""
        from app.parsers.dbs.acc import DbsAccountAdapter

        account = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC)).accounts[0]
        assert not any(t.description_raw.startswith("OTHER") for t in account.txns)

    def test_routes_to_the_dbs_adapter(self, dummy_root):
        document = pdfio.load(_require(dummy_root, DBS_ACC))
        assert build_default_registry().resolve(document).name == "dbs.acc"

    def test_routing_does_not_depend_on_customer_data(self, dummy_root):
        """DBS puts the customer's name, their joint holder's name and their
        street in the header band. None may be required."""
        from app.parsers.dbs.acc import SIGNATURE

        document = pdfio.load(_require(dummy_root, DBS_ACC))
        assert SIGNATURE.matches(document)
        assert not any(
            token in line
            for line in SIGNATURE.requires
            for token in ("example", "lucy", "horizon", "green")
        )


OCBC_CC = "OCBC Bank/cc/OCBC+REWARDS+CARD-0000-Jan-26.pdf"


class TestOcbcCard:
    """The simplest layout in the corpus, with three traps in it: an amount
    column that carries no sign, dates with no year, and a cardholder line that
    is not a row."""

    def _account(self, dummy_root):
        from app.parsers.ocbc.cc import OcbcCardAdapter

        return OcbcCardAdapter().parse(_require(dummy_root, OCBC_CC)).accounts[0]

    def test_reconciles(self, dummy_root):
        from app.parsers.ocbc.cc import OcbcCardAdapter

        parsed = OcbcCardAdapter().parse(_require(dummy_root, OCBC_CC))
        assert parsed.institution == "OCBC"
        account = parsed.accounts[0]
        # Owed, so negated: a card reconciles on the deposit formula.
        assert account.opening_balance_minor == 0
        assert account.closing_balance_minor == -161150
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_the_account_is_the_card_product_not_its_number(self, dummy_root):
        """A card number changes on reissue while the account continues, so
        keying on it would fork one history in two."""
        account = self._account(dummy_root)
        assert account.account_ref_masked == "Ocbc Rewards Card"
        assert "5400" not in account.account_ref_masked

    def test_the_cardholder_line_is_not_part_of_a_description(self, dummy_root):
        """It sits between the product name and the first row, carries no
        amount, and holds the two things a description must never carry."""
        account = self._account(dummy_root)
        for txn in account.txns:
            assert "5400" not in txn.description_raw
            assert "example" not in txn.description_raw.upper()

    def test_an_unsigned_amount_is_a_purchase(self, dummy_root):
        """Nothing in the column says which way; without the CR rule every
        payment would read as another purchase."""
        account = self._account(dummy_root)
        assert all(t.amount_minor < 0 for t in account.txns)
        assert account.txns[0].amount_minor == -7508

    def test_the_year_comes_from_the_statement(self, dummy_root):
        """OCBC prints "20/01" and no year anywhere near the row."""
        from datetime import date

        account = self._account(dummy_root)
        assert account.txns[0].posted_date == date(2026, 1, 20)

    def test_routing_does_not_depend_on_customer_data(self, dummy_root):
        from app.parsers.ocbc.cc import SIGNATURE

        document = pdfio.load(_require(dummy_root, OCBC_CC))
        assert SIGNATURE.matches(document)
        assert not any(
            token in line
            for line in SIGNATURE.requires
            for token in ("example", "sim", "EXAMPLE ROAD", "7552")
        )

    def test_routes_to_the_ocbc_adapter(self, dummy_root):
        document = pdfio.load(_require(dummy_root, OCBC_CC))
        assert build_default_registry().resolve(document).name == "ocbc.cc"


class TestUnregisteredLayouts:
    @pytest.mark.parametrize("relpath", [
        "DBS/cc/-1.pdf",
        "Maribank/acc/Aug2025_MariBank_e-Statement.pdf",
    ])
    def test_open_but_route_nowhere(self, dummy_root, relpath):
        """Institutions without an adapter must still open — several are
        owner-restricted PDFs — and must resolve to no adapter, which is what
        sends them to quarantine instead of a wrong parser."""
        from app.parsers.registry import UnknownLayout

        document = pdfio.load(_require(dummy_root, relpath))
        assert document.pages
        with pytest.raises(UnknownLayout):
            build_default_registry().resolve(document)
