"""Net worth from declared balances.

The cases that matter are the ones where summing transactions would give a
different — and wrong — answer, and the ones where a percentage would be a
confident lie.
"""

from __future__ import annotations

from datetime import date

from app.domain.networth import Declared, change, net_worth


def _d(day, account, minor):
    return Declared(period_end=day, account_id=account, closing_balance_minor=minor)


class TestPosition:
    def test_a_balance_replaces_rather_than_accumulates(self):
        """A balance is a position, not a movement. Adding two statements from
        one account would double it."""
        points = net_worth([
            _d(date(2026, 1, 31), 1, 100_00),
            _d(date(2026, 2, 28), 1, 120_00),
        ], every="statement")
        assert [p.total_minor for p in points] == [100_00, 120_00]

    def test_accounts_carry_forward_between_their_statements(self):
        """Accounts do not close on the same day. Between statements the last
        thing the bank said is the last thing anyone knows."""
        points = net_worth([
            _d(date(2026, 1, 31), 1, 100_00),
            _d(date(2026, 2, 20), 2, 50_00),
        ], every="statement")
        # The second point still counts account 1, which has not reported again.
        assert points[-1].total_minor == 150_00

    def test_a_card_debt_subtracts(self):
        """Card balances are stored negated, so nothing here needs to know
        which kind of account it is looking at."""
        points = net_worth([
            _d(date(2026, 1, 31), 1, 100_00),
            _d(date(2026, 1, 31), 2, -30_00),
        ])
        assert points[0].total_minor == 70_00

    def test_a_point_says_how_much_of_the_picture_it_has(self):
        points = net_worth([
            _d(date(2026, 1, 31), 1, 100_00),
            _d(date(2026, 2, 28), 2, 50_00),
        ], every="statement")
        assert [p.accounts_known for p in points] == [1, 2]

    def test_monthly_keeps_the_last_statement_of_each_month(self):
        """Earlier figures within a month are the same picture with fewer
        accounts updated; plotting them all draws a sawtooth that is an
        artefact of statement timing."""
        points = net_worth([
            _d(date(2026, 1, 20), 1, 100_00),
            _d(date(2026, 1, 31), 2, 40_00),
            _d(date(2026, 2, 28), 1, 110_00),
        ])
        assert [p.on for p in points] == [date(2026, 1, 31), date(2026, 2, 28)]

    def test_nothing_declared_is_no_points(self):
        assert net_worth([]) == []


class TestChange:
    def test_movement_is_end_minus_start(self):
        points = net_worth([
            _d(date(2026, 1, 31), 1, 100_00),
            _d(date(2026, 2, 28), 1, 125_00),
        ])
        assert change(points)["change_minor"] == 25_00
        assert change(points)["percent"] == 0.25

    def test_a_window_opening_in_debt_has_no_percentage(self):
        """There is no honest percent growth from a negative position, and a
        number here carries a feeling — §5.1(D)."""
        points = net_worth([
            _d(date(2026, 1, 31), 1, -50_00),
            _d(date(2026, 2, 28), 1, 10_00),
        ])
        result = change(points)
        assert result["change_minor"] == 60_00
        assert result["percent"] is None

    def test_a_single_point_is_not_a_trend(self):
        points = net_worth([_d(date(2026, 1, 31), 1, 100_00)])
        assert change(points)["percent"] is None
