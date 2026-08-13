"""Reconciliation across statements.

Import-time validation proves one document consistent with itself. These cover
the half that can only be checked afterwards, which is also the half the
`dedupe` module names as its safety net: if an institution reorders same-key
rows between two overlapping statements, a transaction can import twice, and
the claim made in `app/domain/dedupe.py` is that reconciliation catches the
drift. That claim is only worth making if something checks it.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.reconcile import CONTINUITY, MOVEMENT, Declaration, drifts
from app.pipeline.reconcile import check


def declaration(month, opening, closing, account_id=1, end_day=30):
    return Declaration(
        account_id=account_id, account="Test 1",
        period_start=date(2025, month, 1), period_end=date(2025, month, end_day),
        opening_balance_minor=opening, closing_balance_minor=closing,
    )


class TestALedgerThatAgrees:
    def test_a_clean_run_of_statements_reports_nothing(self):
        declarations = [
            declaration(4, 100_00, 150_00),
            declaration(5, 150_00, 120_00),
            declaration(6, 120_00, 200_00),
        ]
        movements = {
            (1, date(2025, 5, 10)): 50_00 - 100_00 + 20_00,   # net -30.00 in May
            (1, date(2025, 6, 10)): 80_00,                    # net +80.00 in June
        }
        assert drifts(declarations, movements) == []

    def test_one_statement_alone_is_not_compared_with_itself(self):
        """Import-time validation already did that, and doing it twice from a
        weaker position would report on documents that declare no balances."""
        assert drifts([declaration(6, 100_00, 200_00)], {}) == []

    def test_a_statement_declaring_no_balances_is_skipped_not_read_as_zero(self):
        """Some layouts genuinely print neither. Those import as
        `imported_unverified`, and treating a null as 0.00 would invent a
        drift the size of the whole account."""
        declarations = [
            declaration(4, 100_00, 150_00),
            declaration(5, None, None),
            declaration(6, 150_00, 150_00),
        ]
        assert drifts(declarations, movements={}) == []

    def test_accounts_do_not_reconcile_against_each_other(self):
        """Two accounts closing in the same month are two runs, not one."""
        declarations = [
            declaration(5, 100_00, 100_00, account_id=1),
            declaration(5, 900_00, 900_00, account_id=2),
            declaration(6, 100_00, 100_00, account_id=1),
            declaration(6, 900_00, 900_00, account_id=2),
        ]
        assert drifts(declarations, movements={}) == []

    def test_statements_out_of_order_are_still_compared_with_their_neighbour(self):
        """Sorted inside, deliberately. An unsorted run would compare January's
        closing with June's opening and report drift on a healthy ledger."""
        declarations = [declaration(6, 150_00, 150_00), declaration(5, 100_00, 150_00)]
        movements = {(1, date(2025, 5, 9)): 50_00}
        assert drifts(declarations, movements) == []


class TestALedgerThatDoesNot:
    def test_a_row_imported_twice_shows_up_as_movement_the_banks_never_declared(self):
        """The failure `dedupe` says this check exists to catch. Two
        overlapping statements listing the same $45 coffee in a different
        order, and the ledger holds it twice."""
        declarations = [declaration(5, 100_00, 100_00), declaration(6, 100_00, 55_00)]
        movements = {
            (1, date(2025, 6, 12)): -45_00,
            (1, date(2025, 6, 13)): -45_00,   # the same purchase, imported again
        }

        found = drifts(declarations, movements)

        assert [d.kind for d in found] == [MOVEMENT]
        assert found[0].declared_minor == -45_00
        assert found[0].observed_minor == -90_00
        assert found[0].difference_minor == -45_00

    def test_a_missing_statement_shows_up_as_a_break_in_continuity(self):
        """April closed at 150.00 and June opened at 300.00. Something happened
        in May, and this ledger has no statement that says what."""
        declarations = [declaration(4, 100_00, 150_00), declaration(6, 300_00, 300_00)]

        found = drifts(declarations, movements={})

        assert CONTINUITY in [d.kind for d in found]
        gap = next(d for d in found if d.kind == CONTINUITY)
        assert (gap.declared_minor, gap.observed_minor) == (150_00, 300_00)
        assert (gap.since, gap.until) == (date(2025, 4, 30), date(2025, 6, 1))

    def test_a_dropped_row_is_caught_as_readily_as_a_duplicated_one(self):
        declarations = [declaration(5, 100_00, 100_00), declaration(6, 100_00, 40_00)]
        movements = {(1, date(2025, 6, 12)): -20_00}   # the bank says -60.00

        found = drifts(declarations, movements)

        assert [d.kind for d in found] == [MOVEMENT]
        assert found[0].difference_minor == 40_00

    def test_the_span_travels_with_the_finding(self):
        """A number with no dates on it sends the operator through every
        statement in the account looking for it."""
        declarations = [declaration(5, 100_00, 100_00), declaration(6, 100_00, 90_00)]

        found = drifts(declarations, movements={})

        assert (found[0].since, found[0].until) == (date(2025, 5, 30), date(2025, 6, 30))

    def test_movement_is_counted_from_the_close_of_one_statement_to_the_next(self):
        """Exclusive at the start and inclusive at the end. A transaction dated
        on the earlier closing date belongs to the statement that declared that
        closing balance, and counting it twice is the bug this pins down."""
        declarations = [declaration(5, 100_00, 100_00), declaration(6, 100_00, 120_00)]
        movements = {
            (1, date(2025, 5, 30)): 999_00,   # already inside May's closing
            (1, date(2025, 6, 30)): 20_00,    # the last day of June counts
        }
        assert drifts(declarations, movements) == []


class TestAgainstARealLedger:
    """The seam between the rules and the database, on both engines."""

    def _statement(
        self, repository, context, *, sha, month, opening, closing, txns, end_day=28,
    ):
        from datetime import datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, BalanceRecord, DocumentRecord, TxnRecord

        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256=sha, institution="Test", doc_type="acc",
                period_start=date(2025, month, 1), period_end=date(2025, month, end_day),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath=f"{sha}.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [BalanceRecord(
                account_key=account,
                opening_balance_minor=opening, closing_balance_minor=closing,
            )],
            [
                TxnRecord(
                    account_key=account, posted_date=on, amount_minor=amount,
                    currency="SGD", description_raw="Thing", description_norm="THING",
                    counterparty_norm="THING", dedupe_key=key, seq=0,
                )
                for on, amount, key in txns
            ],
        )

    def test_a_ledger_that_agrees_reports_nothing(self, repository, context):
        self._statement(
            repository, context, sha="a" * 64, month=5, opening=100_00, closing=150_00,
            txns=[(date(2025, 5, 10), 50_00, "may-1")],
        )
        self._statement(
            repository, context, sha="b" * 64, month=6, opening=150_00, closing=130_00,
            txns=[(date(2025, 6, 10), -20_00, "jun-1")],
        )
        assert check(repository, context) == []

    def test_a_duplicated_transaction_is_found(self, repository, context):
        """Distinct dedupe keys, because that is exactly the case: the key
        differed, so nothing stopped it, and only the balances know."""
        self._statement(
            repository, context, sha="a" * 64, month=5, opening=100_00, closing=100_00,
            txns=[],
        )
        self._statement(
            repository, context, sha="b" * 64, month=6, opening=100_00, closing=55_00,
            txns=[
                (date(2025, 6, 12), -45_00, "jun-1"),
                (date(2025, 6, 12), -45_00, "jun-1-again"),
            ],
        )

        found = check(repository, context)

        assert [d.kind for d in found] == [MOVEMENT]
        assert found[0].difference_minor == -45_00
        assert found[0].account.startswith("Test")

    def test_a_credit_posted_after_its_own_period_does_not_read_as_drift(
        self, repository, context,
    ):
        """The case in this corpus, and the one that would have made the check
        useless. OCBC posts a month-end interest credit on the following day —
        value date 31 JUL, posting date 01 AUG — and counts it in the balance
        the July statement carries forward.

        Bucketed on its own posting date it lands in August, where August's
        declared movement does not expect it, and the account reports drift
        every month forever on a ledger that is exactly right. A check that
        cries wolf monthly is worse than no check: it is the one people learn
        to close without reading.
        """
        self._statement(
            repository, context, sha="a" * 64, month=7, end_day=31,
            opening=100_00, closing=101_00,
            # Dated into August, declared inside July's closing balance.
            txns=[(date(2025, 8, 1), 1_00, "jul-interest")],
        )
        self._statement(
            repository, context, sha="b" * 64, month=8, end_day=31,
            opening=101_00, closing=81_00,
            txns=[(date(2025, 8, 15), -20_00, "aug-1")],
        )

        assert check(repository, context) == []

    def test_the_clamp_does_not_hide_a_real_duplicate(self, repository, context):
        """One-sided and narrow: it moves a date to its own statement's close
        and never further, so a row the ledger holds twice still shows.

        Three statements, because a run's first one has nothing before it to be
        compared against — the duplicate has to sit in a span with a closing
        balance at each end.
        """
        self._statement(
            repository, context, sha="a" * 64, month=6, end_day=30,
            opening=100_00, closing=100_00, txns=[],
        )
        self._statement(
            repository, context, sha="b" * 64, month=7, end_day=31,
            opening=100_00, closing=101_00,
            txns=[(date(2025, 8, 1), 1_00, "jul-interest"),
                  (date(2025, 8, 1), 1_00, "jul-interest-again")],
        )

        found = check(repository, context)

        assert [d.kind for d in found] == [MOVEMENT]
        assert found[0].difference_minor == 1_00

    def test_it_is_scoped_to_the_tenant(self, repository, context, other_context):
        """A neighbouring household's statements must not be able to make this
        one look broken."""
        self._statement(
            repository, other_context, sha="c" * 64, month=5, opening=1_000_00,
            closing=9_999_00, txns=[],
        )
        assert check(repository, context) == []
