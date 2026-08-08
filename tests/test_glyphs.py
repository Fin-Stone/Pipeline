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


def _statements(dummy_root):
    return sorted((dummy_root / "HSBC Bank" / "cc").glob("*.pdf"))


class TestEveryStatementInTheCorpus:
    """Not one sample — all of them, because one was the whole problem.

    The table was built from two statements and keys each character on its
    bitmap *and its pixel size*. Other months set the same characters a little
    larger or smaller, so their bitmaps were absent, and an absent bitmap is
    dropped rather than guessed at. That is the right default and it is silent:
    `225.08` came back as `225.0`, a year as `202`, and four of seven
    statements were rejected or misread.

    So the corpus is swept rather than sampled. A new statement dropped into
    `uploads/dummy` is covered the moment it is there.
    """

    def test_every_statement_reconciles(self, dummy_root):
        found = _statements(dummy_root)
        if not found:
            pytest.skip("no HSBC samples in uploads/dummy/")

        from app.parsers.hsbc.cc import HsbcCardAdapter

        adapter = HsbcCardAdapter()
        wrong = []
        for path in found:
            try:
                account = adapter.parse(path).accounts[0]
            except Exception as exc:
                wrong.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            moved = sum(t.amount_minor for t in account.txns)
            if account.opening_balance_minor + moved != account.closing_balance_minor:
                wrong.append(
                    f"{path.name}: {account.opening_balance_minor} + {moved} "
                    f"!= {account.closing_balance_minor}"
                )
        assert not wrong, "\n".join(wrong)

    def test_the_balances_form_an_unbroken_chain(self, dummy_root):
        """Each statement opens where the last one closed.

        The sharpest check there is, and the one that caught the dropped
        digits: a misread closing balance still reconciles inside its own
        statement if the rows were misread to match, but it cannot agree with
        the *next* statement's opening. Nothing in a single document can
        substitute for it.
        """
        found = _statements(dummy_root)
        if len(found) < 2:
            pytest.skip("need consecutive HSBC statements in uploads/dummy/")

        from app.parsers.hsbc.cc import HsbcCardAdapter

        adapter = HsbcCardAdapter()
        periods = sorted(
            (
                (parsed.period_start, parsed.accounts[0], path.name)
                for path, parsed in ((p, adapter.parse(p)) for p in found)
            ),
        )
        breaks = [
            f"{name} opens at {account.opening_balance_minor} but "
            f"{previous_name} closed at {previous.closing_balance_minor}"
            for (_, previous, previous_name), (_, account, name)
            in zip(periods, periods[1:], strict=False)
            if account.opening_balance_minor != previous.closing_balance_minor
        ]
        assert not breaks, "\n".join(breaks)

    def test_a_quiet_month_is_not_a_failure(self, dummy_root):
        """A card with no activity states the same balance twice and prints no
        rows. Reading that as a parse failure would quarantine a document that
        is perfectly correct."""
        found = _statements(dummy_root)
        if not found:
            pytest.skip("no HSBC samples in uploads/dummy/")

        from app.parsers.hsbc.cc import HsbcCardAdapter

        adapter = HsbcCardAdapter()
        for path in found:
            account = adapter.parse(path).accounts[0]
            if not account.txns:
                assert account.opening_balance_minor == account.closing_balance_minor


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
