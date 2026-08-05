"""Pairing the two legs of a movement between the household's own accounts.

The cases here are the ones that decide whether a spending total is true. A
missed pair overstates spending by the size of the transfer; a wrong pair
understates it by removing something real, which is worse, and is why an
ambiguous pair is refused rather than guessed.
"""

from __future__ import annotations

from datetime import date

from app.domain.transfers import Leg, find_transfers

SAVINGS, CARD, OTHER = 1, 2, 3


def _leg(txn_id, account_id, day, amount, description="", account_ref=""):
    return Leg(
        txn_id=txn_id, account_id=account_id, account_ref=account_ref,
        posted_date=date(2026, 6, day), amount_minor=amount, description=description,
    )


class TestPairing:
    def test_a_card_payment_is_one_movement(self):
        """The payment leaves savings and settles the card. Counting it as
        spending double-counts the purchases it is paying for."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -517840, "PAYMENT TO CARD"),
            _leg(2, CARD, 3, +517840, "PAYMENT BY FAST"),
        ])
        assert len(result.links) == 1
        assert result.links[0].out_txn_id == 1 and result.links[0].in_txn_id == 2
        assert result.linked_txn_ids == {1, 2}

    def test_the_legs_need_not_post_on_the_same_day(self):
        """Two banks book the same movement on their own schedules."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -20000),
            _leg(2, CARD, 5, +20000),
        ])
        assert len(result.links) == 1 and result.links[0].days_apart == 2

    def test_a_gap_too_wide_is_not_a_transfer(self):
        """Past a few days the amounts start colliding with real spending."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -20000),
            _leg(2, CARD, 20, +20000),
        ])
        assert not result.links

    def test_two_movements_inside_one_account_are_not_a_transfer(self):
        """A refund against a purchase on the same card is not money moving
        between accounts."""
        result = find_transfers([
            _leg(1, CARD, 3, -20000),
            _leg(2, CARD, 4, +20000),
        ])
        assert not result.links

    def test_spending_is_left_alone(self):
        result = find_transfers([
            _leg(1, CARD, 3, -4534, "A SHOP"),
            _leg(2, SAVINGS, 3, +500000, "SALARY"),
        ])
        assert not result.links and not result.ambiguous


class TestEvidence:
    def test_a_row_naming_the_other_account_settles_it(self):
        """DBS writes the far account into the row, and where it does the pair
        is not a deduction from amount and date at all."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -20000, "Funds Transfer 98-7654321-0 : I-BANK",
                 account_ref="01-2345678-9"),
            _leg(2, OTHER, 3, +20000, "Advice Funds Transfer 01-2345678-9 : I-BANK",
                 account_ref="98-7654321-0"),
        ])
        assert len(result.links) == 1
        assert result.links[0].evidence == "names the other account"

    def test_the_named_pair_wins_the_counterpart(self):
        """Where one leg names an account and another only matches on amount,
        the named pair claims its counterpart first and the other is left
        unlinked rather than mispaired."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -20000, "Funds Transfer 98-7654321-0",
                 account_ref="01-2345678-9"),
            _leg(2, CARD, 3, -20000, "SOMETHING ELSE"),
            _leg(3, OTHER, 3, +20000, "Advice Funds Transfer 01-2345678-9",
                 account_ref="98-7654321-0"),
        ])
        assert len(result.links) == 1
        assert (result.links[0].out_txn_id, result.links[0].in_txn_id) == (1, 3)


class TestRefusals:
    def test_two_equally_good_counterparts_are_refused(self):
        """Two identical transfers on one day cannot be told apart, and a
        wrong link silently deletes real spending from the total."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -10000),
            _leg(2, CARD, 3, +10000),
            _leg(3, OTHER, 3, +10000),
        ])
        assert not result.links
        assert len(result.ambiguous) == 1
        assert result.ambiguous[0].candidate_txn_ids == (2, 3)

    def test_the_nearer_counterpart_is_not_a_tie(self):
        result = find_transfers([
            _leg(1, SAVINGS, 3, -10000),
            _leg(2, CARD, 4, +10000),
            _leg(3, OTHER, 8, +10000),
        ])
        assert len(result.links) == 1 and result.links[0].in_txn_id == 2

    def test_one_leg_is_claimed_only_once(self):
        """Two outflows cannot both be answered by a single inflow."""
        result = find_transfers([
            _leg(1, SAVINGS, 3, -10000),
            _leg(2, SAVINGS, 3, -10000),
            _leg(3, CARD, 3, +10000),
        ])
        assert len(result.links) == 1
        assert len(result.linked_txn_ids) == 2

    def test_the_outcome_does_not_depend_on_row_order(self):
        """Rows arrive in whatever order the database returns them, and the
        same ledger must always produce the same links."""
        legs = [
            _leg(1, SAVINGS, 3, -10000),
            _leg(2, CARD, 4, +10000),
            _leg(3, OTHER, 6, -10000),
            _leg(4, SAVINGS, 6, +10000),
        ]
        first = find_transfers(legs)
        second = find_transfers(list(reversed(legs)))
        assert [(l.out_txn_id, l.in_txn_id) for l in first.links] == \
               [(l.out_txn_id, l.in_txn_id) for l in second.links]
