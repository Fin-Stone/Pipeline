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

    def test_both_years_share_one_fingerprint(self, dummy_root):
        """Layout identity must survive a change in pocket count (1 vs 3)."""
        a = fingerprinting.fingerprint_pdf(pdfio.load(_require(dummy_root, TRUST_ACC_2024)))
        b = fingerprinting.fingerprint_pdf(pdfio.load(_require(dummy_root, TRUST_ACC_2025)))
        assert a == b
        assert build_default_registry().resolve(a).name == "trust.acc"


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


class TestUnregisteredLayouts:
    @pytest.mark.parametrize("relpath", [
        "DBS/acc/-2.pdf",
        "OCBC Bank/cc/OCBC+REWARDS+CARD-0000-Jan-26.pdf",
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
            build_default_registry().resolve(fingerprinting.fingerprint_pdf(document))
