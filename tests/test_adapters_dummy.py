"""Adapter tests against the operator's real documents.

uploads/dummy is gitignored, so these skip cleanly when the documents are
absent â€” CI stays green without operator data. When they are present, these are
the tests that actually prove the adapters read real statements correctly.

Values asserted here were read directly out of the PDFs.
"""

from __future__ import annotations


import re
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
        # All three pockets share one account number, which is the property
        # under test. The number itself is the operator's and stays out of here.
        assert len({a.account_ref_masked for a in parsed.accounts}) == 1

        # Balances by the relation between them, never by value: these are the
        # operator's, for the same reason the account number above is.
        main = pockets["Main Account"]
        assert main.opening_balance_minor + sum(t.amount_minor for t in main.txns) == main.closing_balance_minor

    def test_credits_are_positive_and_debits_negative(self, dummy_root):
        parsed = TrustAccountAdapter().parse(_require(dummy_root, TRUST_ACC_2025))
        main = next(a for a in parsed.accounts if a.sub_account_label == "Main Account")
        amounts = {t.description_raw: t.amount_minor for t in main.txns}
        # Trust writes "+783.17" for a credit and "2,345.67" bare for a debit.
        # The sign is what the adapter decides; the magnitudes are the
        # operator's and say nothing about whether it decided right.
        assert amounts["FPG"] > 0
        assert amounts["Credit card payment"] < 0
        assert amounts["Interest"] > 0

    def test_single_pocket_statement_also_parses(self, dummy_root):
        parsed = TrustAccountAdapter().parse(_require(dummy_root, TRUST_ACC_2024))
        assert len(parsed.accounts) == 1
        account = parsed.accounts[0]
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_both_years_route_to_the_same_adapter(self, dummy_root):
        """Layout identity must survive a change in pocket count (1 vs 3)."""
        registry = build_default_registry()
        for relpath in (TRUST_ACC_2024, TRUST_ACC_2025):
            document = pdfio.load(_require(dummy_root, relpath))
            assert registry.resolve(document).name == "trust.acc"

    def test_routing_does_not_depend_on_customer_data(self, dummy_root):
        """The signature must name only the bank's own words. Requiring a name
        or an address would mean moving house broke the adapter â€” and would put
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
        # Asserted by shape rather than by value: the account number belongs to
        # the operator's document, and a test is not the place to publish one.
        assert re.fullmatch(r"\d{3}-\d{6}-\d", account.account_ref_masked)
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_the_column_decides_the_direction(self, dummy_root):
        """DBS never signs an amount; withdrawals and deposits are separate
        columns."""
        from app.parsers.dbs.acc import DbsAccountAdapter

        account = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC)).accounts[0]
        amounts = {t.description_raw.split()[0]: t.amount_minor for t in account.txns}
        assert account.txns[0].amount_minor < 0                    # Withdrawal column
        assert any(t.amount_minor > 0 for t in account.txns)        # Deposit column
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

    def test_the_lines_trailing_a_row_are_kept(self, dummy_root):
        """DBS writes who a direct debit paid on the lines *under* it.

        A GIRO collection reads `GIRO Payments / Collections via GIRO` on the
        row and then `ACME`, then the policy number, each on its own line. Those
        were being dropped as page furniture — they are short single-token
        lines, which is also what the rotated registration strip down the left
        margin looks like once it is broken into rows. Thirty-seven of them
        went on one statement, leaving a description of nothing but the
        mechanism and a ledger that could not say who was paid.
        """
        from app.domain.normalise import normalise_counterparty
        from app.parsers.dbs.acc import DbsAccountAdapter

        parsed = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC))
        giro = [
            t for t in parsed.accounts[0].txns
            if t.description_raw.lower().startswith("giro payments")
        ]
        assert giro, "the sample should carry GIRO collections"

        bare = [t for t in giro if normalise_counterparty(t.description_raw) in
                ("GIRO PAYMENTS / COLLECTIONS VIA GIRO", "")]
        assert not bare, (
            "every GIRO collection in this sample names its payee on a "
            f"following line: {[t.description_raw for t in bare]}"
        )

    def test_the_margin_strip_is_still_removed(self, dummy_root):
        """What the dropped rule was actually for. It runs down the page at
        11pt, overlapping rows rather than sitting beside them, so it is
        stripped word by word — dropping the whole line would take the payee
        with it."""
        from app.parsers.dbs.acc import DbsAccountAdapter

        parsed = DbsAccountAdapter().parse(_require(dummy_root, DBS_ACC))
        for txn in parsed.accounts[0].txns:
            assert "PDS_" not in txn.description_raw
            # The strip is reversed text: "POSB Biz Reg No." read backwards.
            assert "BSOP" not in txn.description_raw

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
        # Owed, so negative once negated.
        assert account.closing_balance_minor < 0
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


MARI_CC = "Maribank/cc/Aug2025_Mari_Credit_Card_E-Statement.pdf"
MARI_CC_GROUPED = "Maribank/cc/Feb2026_Mari_Credit_Card_E-Statement.pdf"


class TestMariBankCard:
    """A row printed as three lines, with the values on the middle one and the
    description wrapped above and below it."""

    def _parse(self, dummy_root, relpath=MARI_CC):
        from app.parsers.maribank.cc import MariBankCardAdapter

        return MariBankCardAdapter().parse(_require(dummy_root, relpath))

    @pytest.mark.parametrize("relpath", [MARI_CC, MARI_CC_GROUPED])
    def test_reconciles(self, dummy_root, relpath):
        account = self._parse(dummy_root, relpath).accounts[0]
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_the_description_joins_from_both_sides_of_the_row(self, dummy_root):
        """The merchant is printed above the dated line and the type below."""
        account = self._parse(dummy_root).accounts[0]
        first = account.txns[0]
        assert "zhangjihui002.sg" in first.description_raw
        assert "Instant Checkout" in first.description_raw

    def test_a_category_heading_is_not_part_of_a_description(self, dummy_root):
        """"Purchase" sits over the first row of its section, far enough above
        it that it is a heading and not a wrapped line."""
        account = self._parse(dummy_root).accounts[0]
        assert not account.txns[0].description_raw.startswith("Purchase")

    def test_the_statement_s_own_grouping_is_kept(self, dummy_root):
        """MariBank classifies each row, which is evidence about the row and
        is also what makes the ordering check meaningful."""
        account = self._parse(dummy_root, MARI_CC_GROUPED).accounts[0]
        sections = {t.section for t in account.txns}
        assert "Purchase" in sections and "Repayment/Conversion" in sections

        repayments = [t for t in account.txns if t.section == "Repayment/Conversion"]
        assert repayments and all(t.amount_minor > 0 for t in repayments)

    def test_a_type_line_is_not_mistaken_for_a_heading(self, dummy_root):
        """A repayment is filed under "Repayment/Conversion" and carries the
        type "Repayment" on the line below it. Only position tells them
        apart, and reading the type as a heading loses it from the row."""
        account = self._parse(dummy_root, MARI_CC_GROUPED).accounts[0]
        repayment = next(t for t in account.txns if t.section == "Repayment/Conversion")
        assert "Repayment" in repayment.description_raw

    def test_amounts_carry_their_own_sign(self, dummy_root):
        account = self._parse(dummy_root).accounts[0]
        assert account.txns[0].amount_minor == -4534

    def test_routing_needs_no_vendor_string(self, dummy_root):
        """MariBank publishes neither producer nor creator, so the header
        lines carry routing on their own."""
        from app.parsers.maribank.cc import SIGNATURE

        document = pdfio.load(_require(dummy_root, MARI_CC))
        assert not (document.producer or "").strip()
        assert SIGNATURE.matches(document)
        assert build_default_registry().resolve(document).name == "maribank.cc"

    def test_routing_does_not_depend_on_customer_data(self, dummy_root):
        from app.parsers.maribank.cc import SIGNATURE

        assert not any(
            token in line
            for line in SIGNATURE.requires
            for token in ("example", "sim", "example avenue", "000000")
        )


MARI_ACC = "Maribank/acc/Aug2025_MariBank_e-Statement.pdf"
MARI_ACC_FULL = "Maribank/acc/Feb2026_MariBank_e-Statement.pdf"


class TestMariBankSavings:
    """One document, three products. This reads the savings account and
    recognises the rest by name rather than ignoring what it does not know."""

    def _account(self, dummy_root, relpath=MARI_ACC):
        from app.parsers.maribank.acc import MariBankAccountAdapter

        return MariBankAccountAdapter().parse(_require(dummy_root, relpath)).accounts[0]

    @pytest.mark.parametrize("relpath", [MARI_ACC, MARI_ACC_FULL])
    def test_reconciles(self, dummy_root, relpath):
        account = self._account(dummy_root, relpath)
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_interest_is_recorded_daily(self, dummy_root):
        """A month of accrual, at the resolution the statement published it."""
        account = self._account(dummy_root)
        interest = [t for t in account.txns if t.section == "Savings - Interest Details"]
        assert len(interest) == 31
        assert sum(t.amount_minor for t in interest) == 1598

    def test_the_monthly_interest_posting_is_not_counted_twice(self, dummy_root):
        """February prints the month's interest as a transaction *and* breaks
        it down daily. Keeping both would count the same money twice."""
        account = self._account(dummy_root, MARI_ACC_FULL)
        interest = [t for t in account.txns if t.section == "Savings - Interest Details"]
        assert sum(t.amount_minor for t in interest) == 316
        # The aggregate row carries a month with no day, and is dropped.
        rows = [t for t in account.txns if t.section == "Savings - Transaction Details"]
        assert all("Interest" not in t.description_raw for t in rows)

    def test_the_statement_s_own_totals_agree(self, dummy_root):
        account = self._account(dummy_root, MARI_ACC_FULL)
        # Against the rows rather than against a remembered figure: what this
        # checks is that the statement's own totals match what was parsed out
        # of it, which is the check, and it needs no balance of anybody's.
        out = -sum(t.amount_minor for t in account.txns if t.amount_minor < 0)
        into = sum(t.amount_minor for t in account.txns if t.amount_minor > 0)
        assert account.declared_out_minor == out
        assert account.declared_in_minor == into

    def test_a_fund_purchase_is_read_as_an_expense(self, dummy_root):
        """Investments are cash flows here: the debit is already in the savings
        table, so the units-and-price section adds nothing the ledger holds."""
        account = self._account(dummy_root, MARI_ACC_FULL)
        buys = [t for t in account.txns if "Mari Invest" in t.description_raw]
        assert buys and all(t.amount_minor < 0 for t in buys)

    def test_an_unknown_section_is_refused(self, dummy_root):
        """A product this adapter has never seen must not vanish from a
        document that then reports itself as fully imported."""
        from app.parsers.maribank.acc import MariBankAccountAdapter
        from app.ports.parser import ParseError

        adapter = MariBankAccountAdapter()
        with pytest.raises(ParseError, match="unknown statement section"):
            adapter._classify("SAVINGS", "CRYPTO DETAILS", _FakeLine())

    def test_routes_to_the_savings_adapter(self, dummy_root):
        document = pdfio.load(_require(dummy_root, MARI_ACC))
        assert build_default_registry().resolve(document).name == "maribank.acc"


class _FakeLine:
    page_number = 1
    top = 0.0
    text = "SAVINGS - CRYPTO DETAILS"


OCBC_ACC = "OCBC Bank/acc/0d17cfc5.pdf"


class TestOcbcSavings:
    """The tidiest layout in the corpus: it prints its period, both balances
    and its own totals, so three independent checks are available."""

    def _account(self, dummy_root):
        from app.parsers.ocbc.acc import OcbcAccountAdapter

        return OcbcAccountAdapter().parse(_require(dummy_root, OCBC_ACC)).accounts[0]

    def test_reconciles(self, dummy_root):
        account = self._account(dummy_root)
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_its_own_totals_agree(self, dummy_root):
        account = self._account(dummy_root)
        assert (account.declared_out_minor, account.declared_in_minor) == (0, 180198)

    def test_the_wrapped_category_is_kept(self, dummy_root):
        """A bonus line says only "BONUS INTEREST" on its own row; what it was
        for is on the line beneath, and that is the part worth having."""
        account = self._account(dummy_root)
        assert any("360 CC SPEND BONUS" in t.description_raw for t in account.txns)

    def test_the_card_statement_does_not_claim_it(self, dummy_root):
        """Both are OCBC and both come off the same renderer, so the header
        lines are all that separate a savings statement from a card one."""
        document = pdfio.load(_require(dummy_root, OCBC_ACC))
        assert build_default_registry().resolve(document).name == "ocbc.acc"


class TestOcbcCardVariants:
    """Four card statements, four things one sample could not have shown."""

    @pytest.mark.parametrize("relpath", [
        "OCBC Bank/cc/OCBC+REWARDS+CARD-0000-Jan-26.pdf",
        "OCBC Bank/cc/0f16e5c9.pdf",
        "OCBC Bank/cc/5d61df4f.pdf",
        "OCBC Bank/cc/OCBC REWARDS CARD-0000-May-26.pdf",
    ])
    def test_every_variant_reconciles(self, dummy_root, relpath):
        from app.parsers.ocbc.cc import OcbcCardAdapter

        account = OcbcCardAdapter().parse(_require(dummy_root, relpath)).accounts[0]
        assert account.opening_balance_minor + sum(
            t.amount_minor for t in account.txns
        ) == account.closing_balance_minor

    def test_a_notice_beside_the_product_does_not_hide_the_card(self, dummy_root):
        """The product name shares its line with a penalty-rate notice, and
        the cardholder is pushed 50pt below. Judging the whole line, or
        counting lines rather than distance, found no card at all."""
        from app.parsers.ocbc.cc import OcbcCardAdapter

        parsed = OcbcCardAdapter().parse(
            _require(dummy_root, "OCBC Bank/cc/OCBC REWARDS CARD-0000-May-26.pdf")
        )
        assert [a.account_ref_masked for a in parsed.accounts] == ["Ocbc Rewards Card"]

    def test_parentheses_mean_money_back(self, dummy_root):
        """OCBC marks a credit either with CR or with accounting parentheses.
        Reading only the first turned every refund into another purchase."""
        from app.parsers.ocbc.cc import OcbcCardAdapter

        account = OcbcCardAdapter().parse(
            _require(dummy_root, "OCBC Bank/cc/0f16e5c9.pdf")
        ).accounts[0]
        assert any(t.amount_minor > 0 for t in account.txns)


class TestUnregisteredLayouts:
    @pytest.mark.parametrize("relpath", ["DBS/cc/-1.pdf"])
    def test_open_but_route_nowhere(self, dummy_root, relpath):
        """Institutions without an adapter must still open â€” several are
        owner-restricted PDFs â€” and must resolve to no adapter, which is what
        sends them to quarantine instead of a wrong parser."""
        from app.parsers.registry import UnknownLayout

        document = pdfio.load(_require(dummy_root, relpath))
        assert document.pages
        with pytest.raises(UnknownLayout):
            build_default_registry().resolve(document)
