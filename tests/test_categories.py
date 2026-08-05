"""Deciding what a transaction was for.

The cases that matter are where the rules disagree or fall short, because a
category nobody checks is worse than an admission that the system does not
know: it looks like an answer.
"""

from __future__ import annotations

import pytest

from app.domain.categories import (
    DEFAULT_CATEGORIES,
    UNCATEGORISED,
    Rule,
    categorise,
    coverage,
)


class TestMatching:
    def test_a_rule_decides_a_counterparty(self):
        decision = categorise("FAIRPRICE FINEST", [Rule(r"fairprice", "Grocery")])
        assert decision.category == "Grocery"
        assert decision.is_confident

    def test_nothing_matching_is_not_a_guess(self):
        decision = categorise("SOME NEW SHOP", [Rule(r"fairprice", "Grocery")])
        assert decision.category == UNCATEGORISED
        assert decision.needs_review and decision.rule is None

    def test_matching_ignores_case(self):
        assert categorise("sheng siong", [Rule(r"SHENG SIONG", "Grocery")]).category == "Grocery"

    def test_an_invalid_pattern_is_refused_when_written(self):
        """A broken rule should fail where it is authored, not silently match
        nothing for months."""
        with pytest.raises(ValueError):
            Rule(r"unclosed(", "Grocery")


class TestSpecificity:
    def test_the_narrower_rule_wins(self):
        """IKEA-RESTAURANT is in this corpus and is Dining, not Furnishing.
        The merchant is a strong prior, not an answer."""
        rules = [Rule(r"ikea", "Furnishing"), Rule(r"ikea.restaurant", "Dining")]
        assert categorise("IKEA-RESTAURANT", rules).category == "Dining"
        assert categorise("IKEA TAMPINES", rules).category == "Furnishing"

    def test_weight_overrides_specificity(self):
        """Where the length heuristic is wrong, the operator says so."""
        rules = [
            Rule(r"ikea.restaurant", "Dining"),
            Rule(r"ikea", "Furnishing", weight=10),
        ]
        assert categorise("IKEA-RESTAURANT", rules).category == "Furnishing"


class TestDisagreement:
    def test_two_equal_rules_on_different_categories_is_a_question(self):
        """Resolving it by declaration order would make the answer depend on
        which line of a file came first."""
        rules = [Rule(r"grab", "Transport"), Rule(r"grab", "Dining")]
        decision = categorise("GRAB", rules)

        assert decision.needs_review
        assert decision.rule is None
        assert decision.category == UNCATEGORISED
        assert len(decision.contested) == 2

    def test_two_equal_rules_agreeing_is_not_a_disagreement(self):
        rules = [Rule(r"ntuc", "Grocery"), Rule(r"ntuc", "Grocery")]
        decision = categorise("NTUC", rules)
        assert decision.category == "Grocery" and decision.is_confident

    def test_a_clear_winner_is_not_contested(self):
        rules = [Rule(r"grab", "Transport", weight=5), Rule(r"grab", "Dining")]
        assert categorise("GRAB RIDE", rules).is_confident


class TestCoverage:
    def test_coverage_counts_rows_not_names(self):
        """A rule catching one merchant seen four hundred times is worth four
        hundred rules catching one each."""
        rows = ["FAIRPRICE"] * 8 + ["UNKNOWN SHOP"] * 2
        report = coverage(rows, [Rule(r"fairprice", "Grocery")])

        assert report["rows"] == 10
        assert report["decided"] == 8
        assert report["share"] == 0.8

    def test_the_gaps_are_ranked_by_what_they_would_buy(self):
        rows = ["A"] * 5 + ["B"] * 1
        report = coverage(rows, [])
        assert report["worst_gaps"][0] == ("A", 5)

    def test_contested_rows_are_not_counted_as_decided(self):
        rows = ["GRAB"] * 4
        report = coverage(rows, [Rule(r"grab", "Transport"), Rule(r"grab", "Dining")])
        assert report["decided"] == 0 and report["contested"] == 4


class TestSeedSet:
    def test_the_default_set_is_the_operator_s(self):
        for expected in ("Insurance", "Healthcare", "Furnishing", "Electronics", "Fashion"):
            assert expected in DEFAULT_CATEGORIES

    def test_others_exists_so_a_gap_is_visible(self):
        """Hiding an unclassified row as a null would hide the signal that the
        rules are behind."""
        assert UNCATEGORISED in DEFAULT_CATEGORIES

    def test_retail_is_not_one_bucket(self):
        assert "Shopping" not in DEFAULT_CATEGORIES
