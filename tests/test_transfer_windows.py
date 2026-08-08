"""The window widens with the strength of the evidence.

One window for every kind of pair meant choosing between missing card payments
and guessing at coincidences. Amount and date alone is the weakest thing two
rows can say — on a household ledger the same round number turns up constantly
— so it keeps the tightest bound, while a leg naming the other's account number
and a deposit paying a card can each afford weeks.

The case that motivated all of this: on the operator's real ledger, 44 of 57
unmatched card payments were missing nothing but the other statement.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.domain.models import CARD, DEPOSIT
from app.domain.transfers import Leg, Window, find_transfers


#: Day 1 of the fixtures. Offsets are added to it rather than to a day number,
#: so a test can reach past a month end without arithmetic of its own.
START = date(2026, 6, 1)


def leg(txn_id, account_id, day, amount, *, kind=DEPOSIT, ref="", description=""):
    return Leg(
        txn_id=txn_id, account_id=account_id, account_ref=ref,
        posted_date=START + timedelta(days=day - 1), amount_minor=amount,
        description=description, account_kind=kind,
    )


class TestPayingACard:
    """Money leaving a deposit and landing on a card is a transfer by
    construction: the purchases the card made are already counted as spending,
    so counting the payment too overstates the month by the size of the bill."""

    def _pair(self, gap: int, **window):
        return find_transfers(
            [
                leg(1, 10, 1, -120_00),
                leg(2, 20, 1 + gap, 120_00, kind=CARD),
            ],
            window=Window(**window),
        )

    def test_a_card_payment_pairs_beyond_the_amount_and_date_window(self):
        """Twelve days apart. A card issuer posts on a statement cycle, not on
        the day you paid, and 4 days is nowhere near it."""
        result = self._pair(12)
        assert len(result.links) == 1
        assert result.links[0].evidence == "pays a card"

    def test_the_same_gap_between_two_deposits_does_not_pair(self):
        """Which is the point of separate windows. Without the card rule this
        would need max_days=12 for everyone, and at 12 days unrelated spending
        starts pairing with unrelated income."""
        result = find_transfers(
            [leg(1, 10, 1, -120_00), leg(2, 20, 13, 120_00)],
            window=Window(),
        )
        assert result.links == ()

    def test_a_card_payment_still_has_a_limit(self):
        result = self._pair(40)
        assert result.links == ()

    def test_money_leaving_a_card_is_not_a_payment(self):
        """A refund to a card is not somebody paying their bill. It may still
        pair on amount and date, but not by this rule."""
        result = find_transfers(
            [
                leg(1, 20, 1, -120_00, kind=CARD),
                leg(2, 10, 13, 120_00),
            ],
            window=Window(),
        )
        assert result.links == ()


class TestEvidenceOrder:
    def test_a_named_account_claims_its_counterpart_first(self):
        """Order is what makes three passes safe. A near-proof must settle
        before anything has to guess, or a coincidence takes the row it was
        about to claim."""
        legs = [
            leg(1, 10, 1, -50_00, description="TRANSFER TO 98-7654321-0"),
            leg(2, 20, 1, 50_00, ref="98-7654321-0"),
            leg(3, 30, 1, 50_00),  # equally close, but nothing names it
        ]
        result = find_transfers(legs, window=Window())
        assert [(l.out_txn_id, l.in_txn_id) for l in result.links] == [(1, 2)]
        assert result.links[0].evidence == "names the other account"

    def test_an_ambiguity_in_an_early_pass_is_not_reported_if_a_later_one_settles_it(self):
        """Reporting it would ask the operator to adjudicate something the next
        pass is about to answer."""
        legs = [
            leg(1, 10, 1, -50_00),
            leg(2, 20, 1, 50_00, kind=CARD),
        ]
        result = find_transfers(legs, window=Window())
        assert len(result.links) == 1
        assert result.ambiguous == ()

    def test_a_genuine_tie_is_still_refused(self):
        """A wrong link silently removes real spending from the total, which is
        the failure this whole pass exists to prevent."""
        legs = [
            leg(1, 10, 5, -50_00),
            leg(2, 20, 5, 50_00),
            leg(3, 30, 5, 50_00),
        ]
        result = find_transfers(legs, window=Window())
        assert result.links == ()
        assert [a.txn_id for a in result.ambiguous] == [1]


class TestTheGapBounds:
    def test_min_days_refuses_a_pair_that_is_too_close(self):
        same_day = [leg(1, 10, 3, -50_00), leg(2, 20, 3, 50_00)]
        assert find_transfers(same_day, window=Window()).links != ()
        assert find_transfers(same_day, window=Window(min_days=1)).links == ()

    def test_min_days_applies_to_every_kind_of_evidence(self):
        """Otherwise raising it would look like it had done nothing on a ledger
        whose transfers are mostly card payments."""
        same_day = [leg(1, 10, 3, -50_00), leg(2, 20, 3, 50_00, kind=CARD)]
        assert find_transfers(same_day, window=Window(min_days=2)).links == ()

    def test_widening_finds_more(self):
        legs = [leg(1, 10, 1, -50_00), leg(2, 20, 9, 50_00)]
        assert find_transfers(legs, window=Window()).links == ()
        assert len(find_transfers(legs, window=Window(max_days=10)).links) == 1

    def test_a_min_beyond_every_window_is_refused_rather_than_matching_nothing(self):
        """Silently pairing nothing would look like a working matcher with an
        empty ledger, which is the most expensive kind of wrong."""
        with pytest.raises(ValueError, match="nothing could ever pair"):
            Window(min_days=90)

    def test_a_negative_minimum_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            Window(min_days=-1)


class TestTheOlderCallStillWorks:
    def test_window_days_sets_the_amount_and_date_bound(self):
        """The single-window form was the module's whole interface. Callers that
        never cared about the difference must keep working unchanged."""
        legs = [leg(1, 10, 1, -50_00), leg(2, 20, 7, 50_00)]
        assert find_transfers(legs, window_days=4).links == ()
        assert len(find_transfers(legs, window_days=8).links) == 1

    def test_a_leg_defaults_to_a_deposit(self):
        """So a caller that has not been taught about account kinds yet builds
        legs that behave exactly as they did before."""
        assert Leg(1, 1, "", START, -1, "").account_kind == DEPOSIT
