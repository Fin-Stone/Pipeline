"""Reading a PDF whose text is drawn rather than typed.

HSBC's generator emits every character as a one-bit bitmap and embeds no
fonts, so `pdfplumber` and `pypdf` both extract zero characters. These cover
the reconstruction — and, more importantly, the two things that would make it
dangerous rather than merely wrong: a misread digit, and text invented where
there was none.
"""

from __future__ import annotations

import pytest

from app.parsers import glyphs

pytestmark = pytest.mark.requires_dummy


class TestTheAlphabet:
    def test_a_table_ships_with_the_parser(self):
        table = glyphs.load_tables()
        assert len(table) > 300, "the HSBC alphabet should be loaded"

    def test_a_digest_covers_the_size_as_well_as_the_pixels(self):
        """The same pixel run at two widths is two different pictures, and a
        bare content hash would read one as the other."""
        raw = bytes([0b10101010]) * 8
        assert glyphs.digest_of(raw, 8, 8) != glyphs.digest_of(raw, 4, 16)

    def test_an_unknown_glyph_is_dropped_rather_than_guessed(self):
        """A character invented here would be indistinguishable downstream from
        one that was really on the statement."""
        assert glyphs.load_tables().get("0" * 16) is None


class TestWhenItEngages:
    class _Page:
        def __init__(self, chars, images):
            self.chars = chars
            self.images = images

    def test_a_page_with_real_text_is_left_alone(self):
        page = self._Page(["x"] * 500, [{"bits": 1}] * 5000)
        assert glyphs.page_needs_glyphs(page) is False

    def test_a_page_of_pictures_and_nothing_else_is_decoded(self):
        page = self._Page([], [{"bits": 1}] * 5000)
        assert glyphs.page_needs_glyphs(page) is True

    def test_a_scanned_page_with_one_image_is_not(self):
        """This is for reconstructing drawn text, not for OCR of a photograph."""
        page = self._Page([], [{"bits": 1}])
        assert glyphs.page_needs_glyphs(page) is False

    def test_a_page_with_a_footer_and_thousands_of_glyphs_is_decoded(self):
        """Some generators emit a handful of real characters beside the
        bitmaps. A page that is 99% pictures has no text whatever the last few
        characters claim."""
        page = self._Page(["1", "2"], [{"bits": 1}] * 5000)
        assert glyphs.page_needs_glyphs(page) is True


class TestReadingAnHsbcStatement:
    """The end-to-end claim, on the real document.

    **The balance check is the proof.** A statement whose amounts were misread
    does not reconcile, so `reconciles: YES` on a document with no text in it
    says the digits came back exactly right — which is the whole difference
    between this and OCR.
    """

    @pytest.fixture
    def parsed(self, dummy_root):
        from app.parsers.hsbc.cc import HsbcCardAdapter

        path = dummy_root / "HSBC Bank" / "cc" / "2026-02-21_Statement.pdf"
        if not path.exists():
            pytest.skip("no HSBC sample in uploads/dummy/")
        return HsbcCardAdapter().parse(path)

    def test_the_statement_reconciles(self, parsed):
        account = parsed.accounts[0]
        moved = sum(t.amount_minor for t in account.txns)
        assert account.opening_balance_minor + moved == account.closing_balance_minor

    def test_the_card_is_masked_to_its_last_four(self, parsed):
        """The full number is on the statement and has no business in a ledger."""
        reference = parsed.accounts[0].account_ref_masked
        assert reference.startswith("xxxx-xxxx-xxxx-")
        assert sum(c.isdigit() for c in reference) == 4

    def test_a_payment_is_money_coming_back_not_another_purchase(self, parsed):
        """HSBC glues its credit marker to the figure — `5'000.00cR` — so a
        word-boundary match finds nothing and reads the card payment as a
        second purchase, wrong by twice the bill."""
        assert any(t.amount_minor > 0 for t in parsed.accounts[0].txns)

    def test_the_period_is_taken_from_the_statement_not_guessed(self, parsed):
        assert parsed.period_start < parsed.period_end
        assert all(
            parsed.period_start <= t.posted_date <= parsed.period_end
            for t in parsed.accounts[0].txns
        )

    def test_no_prose_is_folded_into_a_description(self, parsed):
        """The third page repeats a block of terms and conditions, and one of
        its lines sits as close under a transaction as a genuine wrap does. A
        wrap stays inside the description column; a paragraph does not."""
        assert not any(
            "immediately" in t.description_raw or "Transaction details" in t.description_raw
            for t in parsed.accounts[0].txns
        )

    def test_thousands_are_read_through_the_apostrophe(self, parsed):
        """HSBC writes 4'000.00, not 4,285.70. Read as `4.00` the statement
        would still parse and be wrong by four thousand dollars."""
        assert parsed.accounts[0].opening_balance_minor == -428570
