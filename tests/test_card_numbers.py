"""Recognising a bill payment as settling the card it names.

A card account is keyed by its *product* — "Ocbc Rewards Card" — because the
number changes on reissue and keying on it would fork one card's history in
two. That identity is right, and it leaves a hole: the deposit statement paying
the bill records the **number**, so nothing joined the payment to the card.

The payment then either went unmatched, which reads as money leaving the
household for nowhere, or was matched on amount alone, which cannot tell two
cards apart when both are paid in the same month.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.models import CARD, DEPOSIT
from app.domain.transfers import Leg, find_transfers
from app.parsers import cards

SAVINGS, THE_CARD = 1, 2


def _leg(txn_id, account_id, day, amount, description="", *, kind=DEPOSIT, numbers=()):
    return Leg(
        txn_id=txn_id, account_id=account_id, account_ref="Ocbc Rewards Card",
        posted_date=date(2026, 6, day), amount_minor=amount, description=description,
        account_kind=kind, card_numbers=numbers,
    )


class TestReadingTheNumberOffAStatement:
    """Three banks, three shapes, one reader."""

    @pytest.mark.parametrize("text, expected", [
        ("Card No. 4111 1111 1111 4321", "xxxx-xxxx-xxxx-4321"),
        ("4111111111114321", "xxxx-xxxx-xxxx-4321"),
        ("Card ending ************8765", "xxxx-xxxx-xxxx-8765"),
        ("MARI CREDIT CARD ****2468", "xxxx-xxxx-xxxx-2468"),
        ("xxxx-xxxx-xxxx-4321", "xxxx-xxxx-xxxx-4321"),
    ])
    def test_the_shapes_banks_actually_use(self, text, expected):
        assert cards.find(text) == expected

    @pytest.mark.parametrize("text", [
        "Statement period 01 Jan 2026 to 31 Jan 2026",
        "Total due 1'234.56",
        "Reference 8891",
    ])
    def test_what_is_not_a_card_number(self, text):
        assert cards.find(text) is None

    def test_only_the_last_four_survive(self):
        """A full card number has no business in a household ledger, and the
        last four is everything the matching needs."""
        found = cards.find("4012 8888 5678 4321")
        assert "4012" not in found and "8888" not in found and "5678" not in found
        assert sum(c.isdigit() for c in found) == 4

    def test_a_statement_naming_one_card_twice_yields_one(self):
        """Printed in the header and again beside the payment slip."""
        assert cards.find_all([
            "Card No. 4111 1111 1111 4321",
            "Please quote 4111 1111 1111 4321 when paying",
        ]) == ("xxxx-xxxx-xxxx-4321",)


class TestPairingAPaymentWithItsCard:
    def test_a_payment_naming_the_number_settles_that_card(self):
        result = find_transfers([
            _leg(1, SAVINGS, 3, -500000, "Advice Bill Payment CCC - 4111111111114321 : I-BANK"),
            _leg(2, THE_CARD, 3, +500000, "PAYMENT BY INTERNET",
                 kind=CARD, numbers=("xxxx-xxxx-xxxx-4321",)),
        ])
        assert len(result.links) == 1
        assert result.links[0].evidence == "names the card it pays"

    def test_a_number_the_card_used_to_carry_still_counts(self):
        """The whole reason the numbers are a set that grows. Reissued twice,
        and a payment made to the first number still settled this card."""
        history = (
            "xxxx-xxxx-xxxx-1111", "xxxx-xxxx-xxxx-2222", "xxxx-xxxx-xxxx-3333",
        )
        for old in ("4111111111111111", "4111111111112222", "4111111111113333"):
            result = find_transfers([
                _leg(1, SAVINGS, 3, -20000, f"Advice Bill Payment CCC - {old} : I-BANK"),
                _leg(2, THE_CARD, 3, +20000, "PAYMENT", kind=CARD, numbers=history),
            ])
            assert len(result.links) == 1, f"payment to {old[-4:]} should settle the card"

    def test_the_named_card_wins_over_the_one_merely_paid_the_same_day(self):
        """Two cards, both paid the same amount in the same month. Amount and
        direction cannot tell them apart; the number can, and a wrong link here
        removes real spending from the total."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -100000, "Advice Bill Payment CCC - 4111111111114321"),
            _leg(2, THE_CARD, 3, +100000, "PAYMENT", kind=CARD,
                 numbers=("xxxx-xxxx-xxxx-1111",)),
            _leg(3, 3, 3, +100000, "PAYMENT", kind=CARD,
                 numbers=("xxxx-xxxx-xxxx-4321",)),
        ])
        assert {(link.out_txn_id, link.in_txn_id) for link in result.links} == {(1, 3)}

    def test_a_card_with_no_number_on_record_still_pairs_structurally(self):
        """Trust prints no card number anywhere on its statement. Nothing
        regresses: the payment is still a deposit paying a card."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -30000, "Credit card payment"),
            _leg(2, THE_CARD, 5, +30000, "Repayment", kind=CARD),
        ])
        assert len(result.links) == 1
        assert result.links[0].evidence == "pays a card"

    def test_a_short_reference_is_not_mistaken_for_a_card(self):
        """A reference number's four digits look exactly like a card's tail.
        One of them is twelve digits longer, and that is the whole difference —
        without it every payment quoting a reference could claim any card."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -20000, "Advice Funds Transfer REF: 4321"),
            _leg(2, THE_CARD, 3, +20000, "PAYMENT", kind=CARD,
                 numbers=("xxxx-xxxx-xxxx-4321",)),
        ])
        assert result.links[0].evidence != "names the card it pays"


class TestRememberingThem:
    """The numbers accumulate in the ledger, statement by statement."""

    def _import(self, repository, context, *, sha, numbers, period_end, amount=-10000):
        from datetime import datetime, timezone

        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        account = AccountRecord(
            institution="OCBC", account_ref_masked="Ocbc Rewards Card",
            currency="SGD", kind=CARD, card_numbers=numbers,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256=sha, institution="OCBC", doc_type="cc",
                period_start=date(period_end.year, period_end.month, 1),
                period_end=period_end, storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath=f"{sha}.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [TxnRecord(
                account_key=account, posted_date=period_end, amount_minor=amount,
                currency="SGD", description_raw="x", description_norm="x",
                counterparty_norm="", dedupe_key=sha[:32], seq=0,
            )],
        )

    def _stored(self, repository, context):
        legs = repository.list_transfer_legs(context)
        return set(legs[0]["card_numbers"])

    def test_a_reissue_adds_a_number_rather_than_replacing_one(self, repository, context):
        self._import(repository, context, sha="a" * 64,
                     numbers=("xxxx-xxxx-xxxx-1111",), period_end=date(2026, 1, 31))
        self._import(repository, context, sha="b" * 64,
                     numbers=("xxxx-xxxx-xxxx-2222",), period_end=date(2026, 2, 28),
                     amount=-20000)
        assert self._stored(repository, context) == {
            "xxxx-xxxx-xxxx-1111", "xxxx-xxxx-xxxx-2222",
        }

    def test_the_same_number_on_twelve_statements_is_recorded_once(self, repository, context):
        for month in range(1, 4):
            self._import(
                repository, context, sha=chr(ord("a") + month) * 64,
                numbers=("xxxx-xxxx-xxxx-4321",),
                period_end=date(2026, month, 28), amount=-1000 * month,
            )
        assert self._stored(repository, context) == {"xxxx-xxxx-xxxx-4321"}

    def test_a_card_number_is_not_part_of_the_account_identity(self, repository, context):
        """The reason for all of this. Two statements, two numbers, one card —
        keying on the number would have made it two accounts and split the
        history down the middle."""
        self._import(repository, context, sha="c" * 64,
                     numbers=("xxxx-xxxx-xxxx-1111",), period_end=date(2026, 1, 31))
        self._import(repository, context, sha="d" * 64,
                     numbers=("xxxx-xxxx-xxxx-2222",), period_end=date(2026, 2, 28),
                     amount=-20000)
        assert len({leg["account_id"] for leg in repository.list_transfer_legs(context)}) == 1

    def test_a_ledger_gains_them_without_a_reparse(self, repository, context):
        """The reason `record_card_numbers` exists at all.

        A reparse would collect these too, and would assign new transaction ids
        doing it — silently discarding every categorisation, hidden row and
        manual transfer mark attached to the old ones. Years of a person's
        decisions, to gain a field that changes no figure by itself.
        """
        from app.ports.repository import AccountRecord

        self._import(repository, context, sha="1" * 64,
                     numbers=(), period_end=date(2026, 1, 31))
        before = repository.list_transfer_legs(context)
        repository.replace_rule_enrichments(context, [(before[0]["id"], "Dining", 1.0)])
        repository.hide_txn(context, before[0]["id"], note="")

        added = repository.record_card_numbers(context, [AccountRecord(
            institution="OCBC", account_ref_masked="Ocbc Rewards Card",
            currency="SGD", kind=CARD, card_numbers=("xxxx-xxxx-xxxx-4321",),
        )], date(2026, 1, 31))

        after = repository.list_transfer_legs(context)
        assert added == 1
        assert after[0]["card_numbers"] == ("xxxx-xxxx-xxxx-4321",)
        assert [row["id"] for row in after] == [row["id"] for row in before], \
            "transaction ids must survive, or everything hanging off them is lost"
        assert repository.list_hidden(context)[0]["id"] == before[0]["id"]

    def test_running_the_backfill_twice_records_nothing_the_second_time(self, repository, context):
        from app.ports.repository import AccountRecord

        self._import(repository, context, sha="2" * 64,
                     numbers=(), period_end=date(2026, 1, 31))
        record = AccountRecord(
            institution="OCBC", account_ref_masked="Ocbc Rewards Card",
            currency="SGD", kind=CARD, card_numbers=("xxxx-xxxx-xxxx-4321",),
        )
        assert repository.record_card_numbers(context, [record], date(2026, 1, 31)) == 1
        assert repository.record_card_numbers(context, [record], date(2026, 1, 31)) == 0

    def test_a_statement_whose_account_is_not_here_invents_nothing(self, repository, context):
        """An account is something statements create by being imported, not
        something a backfill conjures from a card number."""
        from app.ports.repository import AccountRecord

        added = repository.record_card_numbers(context, [AccountRecord(
            institution="Nowhere", account_ref_masked="Unknown Card",
            currency="SGD", kind=CARD, card_numbers=("xxxx-xxxx-xxxx-0000",),
        )], date(2026, 1, 31))

        assert added == 0
        assert repository.list_accounts(context) == []

    def test_statements_uploaded_out_of_order_widen_the_span(self, repository, context):
        """An operator uploads whatever they find, in whatever order."""
        self._import(repository, context, sha="e" * 64,
                     numbers=("xxxx-xxxx-xxxx-4321",), period_end=date(2026, 6, 30))
        self._import(repository, context, sha="f" * 64,
                     numbers=("xxxx-xxxx-xxxx-4321",), period_end=date(2026, 1, 31),
                     amount=-20000)
        from sqlalchemy import select

        from app.storage import schema
        with repository.engine.connect() as conn:
            row = conn.execute(select(
                schema.account_card_number.c.first_seen,
                schema.account_card_number.c.last_seen,
            )).one()
        assert (row.first_seen, row.last_seen) == (date(2026, 1, 31), date(2026, 6, 30))
