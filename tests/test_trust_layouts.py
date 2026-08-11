"""Trust layout handling, including the two-date layout used from 2025.

Every case here was reconstructed from redacted failure reports — column
contents, y-positions and row pitch — without access to the statements
themselves. That is the property the diagnostics were built for, and these
fixtures are the proof it works.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.dates import DateParseError, resolve_near_period
from app.parsers import pdfio
from app.parsers.fingerprint import normalise_producer
from app.parsers.trust import base
from app.ports.parser import ParseError

from .fixtures.make_pdf import synthetic_statement, two_date_statement, write_pdf


def _rows(tmp_path, placements, name="stmt.pdf"):
    path = write_pdf(tmp_path / name, placements)
    document = pdfio.load(path)
    bands = base.header_bands(base.find_header(document))
    lines = [l for l in base.transaction_lines(document) if not base.is_skippable(l)]
    return base.assemble_rows(lines, bands)


class TestTwoDateLayout:
    """From 2025 Trust prints a transaction date beside the posting date."""

    PERIOD = (date(2024, 6, 1), date(2024, 6, 30))

    def test_posting_date_is_the_second_one(self, tmp_path):
        rows = _rows(tmp_path, two_date_statement(
            [("28 May", "01 Jun", "Bus / MRT", "4.40")],
        ))
        txn = base.build_txn(rows[1], *self.PERIOD)
        assert txn.posted_date == date(2024, 6, 1)
        assert txn.value_date == date(2024, 5, 28)

    def test_transaction_date_before_the_period_is_accepted(self, tmp_path):
        """A purchase on 28 May posting on 1 Jun is normal, not an error."""
        rows = _rows(tmp_path, two_date_statement(
            [("28 May", "01 Jun", "Groceries", "12.00")],
        ))
        txn = base.build_txn(rows[1], *self.PERIOD)
        assert txn.value_date == date(2024, 5, 28)
        assert self.PERIOD[0] <= txn.posted_date <= self.PERIOD[1]

    def test_year_boundary_resolves_to_the_previous_year(self, tmp_path):
        """The real case: '29 Dec 02 Jan' on a January statement."""
        rows = _rows(tmp_path, two_date_statement(
            [("29 Dec", "02 Jan", "Bus / MRT", "2.56")],
            opening=("01 Jan", "01 Jan", "636.11"),
            closing=("31 Jan", "31 Jan", "638.67"),
            period="1 Jan 2026 - 31 Jan 2026",
        ))
        txn = base.build_txn(rows[1], date(2026, 1, 1), date(2026, 1, 31))
        assert txn.posted_date == date(2026, 1, 2)
        assert txn.value_date == date(2025, 12, 29)

    def test_both_dates_equal_still_works(self, tmp_path):
        rows = _rows(tmp_path, two_date_statement(
            [("03 Jun", "03 Jun", "Coffee", "4.50")],
        ))
        txn = base.build_txn(rows[1], *self.PERIOD)
        assert txn.posted_date == txn.value_date == date(2024, 6, 3)

    def test_balance_rows_are_recognised(self, tmp_path):
        rows = _rows(tmp_path, two_date_statement(
            [("03 Jun", "03 Jun", "Coffee", "4.50")],
        ))
        assert rows[0].label == "previous balance"
        assert rows[-1].label == "total outstanding balance"

    def test_a_document_reconciles_end_to_end(self, tmp_path):
        """4.24 owed, 561.35 spent, 559.00 repaid -> 6.59 owed."""
        rows = _rows(tmp_path, two_date_statement(
            [("28 May", "01 Jun", "Purchases", "561.35"),
             ("10 Jun", "10 Jun", "FAST Credit Payment", "+559.00")],
            opening=("01 Jun", "01 Jun", "4.24"),
            closing=("30 Jun", "30 Jun", "6.59"),
        ))
        opening = base.balance_amount(rows[0].sgd_text, owed_is_negative=True)
        closing = base.balance_amount(rows[-1].sgd_text, owed_is_negative=True)
        txns = [base.build_txn(r, *self.PERIOD) for r in rows[1:-1]]
        assert opening + sum(t.amount_minor for t in txns) == closing


class TestSingleDateLayoutStillWorks:
    """The 2022-2023 statements must not regress."""

    def test_single_date_row_parses(self, tmp_path):
        rows = _rows(tmp_path, synthetic_statement(
            [("03 Jun", "Salary", "+2,000.00")], opening="1,000.00", closing="3,000.00",
        ))
        txn = base.build_txn(rows[1], date(2024, 6, 1), date(2024, 6, 30))
        assert txn.posted_date == date(2024, 6, 3)
        # Nothing to infer a transaction date from, so none is invented.
        assert txn.value_date is None


class TestWrappedDescriptions:
    PERIOD = (date(2024, 6, 1), date(2024, 6, 30))

    def test_a_continuation_below_attaches_to_its_own_row(self, tmp_path):
        """Previously it was held over and prepended to the *next* row,
        corrupting both descriptions and therefore both dedupe keys."""
        rows = _rows(tmp_path, two_date_statement(
            [("03 Jun", "03 Jun", "", "32.70"),
             ("05 Jun", "05 Jun", "Coffee", "4.50")],
            lead_ins={0: "LONG MERCHANT NAME"},
            continuations={0: "SINGAPORE SG"},
        ))
        wrapped, following = rows[1], rows[2]
        assert "LONG MERCHANT NAME" in wrapped.description
        assert "SINGAPORE SG" in wrapped.description
        assert following.description == "Coffee"

    def test_a_lead_in_still_attaches_to_the_row_below(self, tmp_path):
        rows = _rows(tmp_path, two_date_statement(
            [("03 Jun", "03 Jun", "", "32.70")],
            lead_ins={0: "MERCHANT ABOVE"},
        ))
        assert "MERCHANT ABOVE" in rows[1].description

    def test_an_address_wrapping_onto_two_lines_stays_with_its_row(self, tmp_path):
        """A merchant's address wraps onto a second line 9.005pt under the
        first, and the threshold was 9.0 — measured from the row, so the second
        line sat 18pt away and fell out entirely.

        Both halves then went wrong at once: the address was held over and
        prepended to the row below, so a savings statement recorded its June
        interest credit as "SAMPLE CIRCLE #01-01 GROCER HUB SINGAPORE 000000
        ..ID:T00XX0000X Interest". Two descriptions corrupted, and
        `description_norm` feeds the dedupe key.
        """
        rows = _rows(tmp_path, two_date_statement(
            [("27 Jun", "27 Jun", "EXAMPLE ENTERPRISE MALL", "32.70"),
             ("30 Jun", "30 Jun", "Interest", "1.16")],
            continuations={0: ["SAMPLE CIRCLE #01-01 GROCER HUB", "000000 ..ID:T00XX0000X"]},
        ))
        wrapped, following = rows[1], rows[2]
        assert wrapped.description == (
            "EXAMPLE ENTERPRISE MALL SAMPLE CIRCLE #01-01 GROCER HUB 000000 ..ID:T00XX0000X"
        )
        assert following.description == "Interest"

    def test_a_held_fragment_does_not_attach_to_a_distant_row(self, tmp_path):
        """What is left pending has to sit just above the row that claims it.
        Without a lower bound anything held over joined whatever came next,
        however far away it had been printed."""
        rows = _rows(tmp_path, two_date_statement(
            [("03 Jun", "03 Jun", "Coffee", "4.50"),
             ("05 Jun", "05 Jun", "Groceries", "12.00")],
            # Four wrap-offsets down: too far under its own row to be a
            # continuation, and too far above the next to lead into it.
            continuations={0: ["", "", "", "STRAY FOOTER TEXT"]},
        ))
        assert "STRAY FOOTER TEXT" not in rows[1].description
        assert "STRAY FOOTER TEXT" not in rows[2].description


class TestDateResolution:
    def test_lookback_accepts_a_date_before_the_period(self):
        assert resolve_near_period("29 Dec", date(2026, 1, 1), date(2026, 1, 31)) == date(2025, 12, 29)

    def test_lookback_still_rejects_something_far_outside(self):
        with pytest.raises(DateParseError):
            resolve_near_period("15 Jun", date(2026, 1, 1), date(2026, 1, 31))

    def test_lookback_cannot_introduce_ambiguity(self):
        """Under a year of lookback, a day-and-month has exactly one candidate."""
        assert resolve_near_period("05 Jan", date(2026, 1, 1), date(2026, 1, 31)) == date(2026, 1, 5)


class TestProducerNormalisation:
    def test_a_browser_upgrade_does_not_change_the_layout(self):
        """Trust's producer moved from Skia/PDF m80 to m141 on a Chromium
        upgrade, which was enough to quarantine an unchanged statement."""
        assert normalise_producer("Skia/PDF m80") == normalise_producer("Skia/PDF m141")

    def test_different_tools_still_differ(self):
        assert normalise_producer("Skia/PDF m80") != normalise_producer("Streamline PDFGen for OCBC Group")

    def test_handles_missing_metadata(self):
        assert normalise_producer("") == ""
        assert normalise_producer(None) == ""


class TestNoDateAtAll:
    def test_a_row_without_a_date_is_a_loud_failure(self, tmp_path):
        rows = _rows(tmp_path, two_date_statement([("03 Jun", "03 Jun", "Coffee", "4.50")]))
        broken = base.Row(rows[1].line, "", "Coffee", "", "4.50")
        with pytest.raises(ParseError, match="no date found"):
            base.build_txn(broken, date(2024, 6, 1), date(2024, 6, 30))


class TestProducerTokens:
    """The rendering tool is a stable signal; its branding is not."""

    def test_a_renamed_vendor_still_matches(self):
        """DBS's creator went from "Quadient Group AG~Inspire" to
        "Quadient CXM AG~Inspire" to "Quadient~Inspire". One product, a company
        that renamed itself twice, 107 statements that stopped routing."""
        from app.parsers.fingerprint import _has_tokens

        required = ("quadient", "inspire")
        for creator in (
            "Quadient Group AG~Inspire~12.5.33.0",
            "Quadient CXM AG~Inspire~15.0.681.5",
            "Quadient~Inspire~17.0.612.15",
        ):
            assert _has_tokens(creator, required), creator

    def test_a_different_tool_does_not_match(self):
        from app.parsers.fingerprint import _has_tokens

        assert not _has_tokens("Skia/PDF m141", ("quadient", "inspire"))
        assert not _has_tokens("Streamline PDFGen for OCBC Group", ("quadient", "inspire"))

    def test_chromium_versions_still_match(self):
        from app.parsers.fingerprint import _has_tokens

        assert _has_tokens("Skia/PDF m80", ("skia", "pdf"))
        assert _has_tokens("Skia/PDF m141", ("skia", "pdf"))

    def test_absent_metadata_matches_only_an_empty_requirement(self):
        from app.parsers.fingerprint import _has_tokens

        assert _has_tokens("", ())
        assert not _has_tokens("", ("quadient",))
