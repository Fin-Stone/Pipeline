"""Deciding what a transaction was for.

The cases that matter are where the rules disagree or fall short, because a
category nobody checks is worse than an admission that the system does not
know: it looks like an answer.
"""

from __future__ import annotations

import pytest

from app.domain.categories import (
    DEFAULT_CATEGORIES,
    SAMPLE_LIMIT,
    UNCATEGORISED,
    Proposal,
    Rule,
    RuleSet,
    categorise,
    consolidate,
    coverage,
    gate_amount,
    is_personal,
    operator_rule,
    propose,
    redact_description,
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


class TestRuleSet:
    """An index over the exact-match rules.

    It exists only to be faster, so the thing worth testing is that it is never
    *different*. Every case here compares it against the plain scan it replaced
    — a fast path that disagrees is not a fast path, it is a bug that only
    shows up on the installs with enough rules to matter.
    """

    def _both(self, name, rules):
        return categorise(name, list(rules)), categorise(name, RuleSet(rules))

    def test_a_decided_counterparty_is_recognised_as_exact(self):
        """`operator_rule` is what generates nearly every rule on a working
        install, so its output is the shape the index has to recognise."""
        pattern, category, weight, note = operator_rule("AUNTIE ANNE'S JEWEL", "Dining")
        assert Rule(pattern, category, weight, note).literal == "AUNTIE ANNE'S JEWEL"

    def test_a_real_expression_is_not_indexed(self):
        assert Rule(r"fairprice", "Grocery").literal is None
        assert Rule(r"^IKEA.*", "Furnishing").literal is None
        assert Rule(r"^(A|B)$", "Other").literal is None

    def test_an_exact_rule_still_matches_case_insensitively(self):
        rules = [Rule(*operator_rule("SHENG SIONG", "Grocery"))]
        scanned, indexed = self._both("sheng siong", rules)
        assert scanned.category == indexed.category == "Grocery"

    def test_an_exact_rule_does_not_match_a_longer_name(self):
        """`^X$` is a whole-name claim. A dict keyed on X must not answer for
        'X TAMPINES', which is exactly what a substring index would do."""
        rules = [Rule(*operator_rule("IKEA", "Furnishing"))]
        scanned, indexed = self._both("IKEA TAMPINES", rules)
        assert scanned.rule is None and indexed.rule is None

    def test_exact_and_general_rules_are_both_found(self):
        rules = [Rule(*operator_rule("GRAB", "Transport")), Rule(r"grab", "Other", weight=5)]
        scanned, indexed = self._both("GRAB", rules)
        assert scanned.category == indexed.category

    def test_two_exact_rules_on_one_name_still_contest(self):
        """Contest detection reads every rule that matched, not the first. An
        index returning one of them would silently settle a disagreement."""
        rules = [
            Rule(*operator_rule("SOMEWHERE", "Dining")),
            Rule(*operator_rule("SOMEWHERE", "Grocery")),
        ]
        scanned, indexed = self._both("SOMEWHERE", rules)
        assert scanned.contested and indexed.contested
        assert scanned.category == indexed.category == UNCATEGORISED

    def test_an_unmatched_name_is_unmatched_either_way(self):
        rules = [Rule(*operator_rule("SOMEWHERE", "Dining"))]
        scanned, indexed = self._both("ELSEWHERE", rules)
        assert scanned.rule is None and indexed.rule is None

    def test_it_is_a_sequence_of_the_rules_it_was_given(self):
        """Passed anywhere a list of rules goes today, so nothing else needs to
        know it exists."""
        rules = [Rule(r"a", "Dining"), Rule(r"b", "Grocery")]
        assert list(RuleSet(rules)) == rules
        assert len(RuleSet(rules)) == 2


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


class TestSanitisation:
    """What may leave the household, and what may not.

    The exclusions are not interchangeable: one is about what the task needs,
    the other about whose privacy is actually at stake.
    """

    def test_a_person_is_never_proposed(self):
        """This corpus holds "FROM: A N OTHER". Scrubbing the operator's own
        identifiers while sending the names of people they pay inverts the
        protection."""
        rows = [("FAST PAYMENT FROM: A N OTHER", -2500)] * 5
        assert propose(rows) == []

    def test_a_person_is_never_proposed_after_normalisation_either(self):
        """The above tests a shape the pipeline does not produce, and that is
        how real names got out. `normalise_counterparty` strips `PAYNOW` and
        `TO:` as mechanism and the reference as digits, so what reaches
        `propose` is `A N OTHER` — nothing left for `is_personal` to find. The
        judgement has to be made on the line, which still says how they were
        paid.
        """
        from app.domain.normalise import normalise_counterparty

        raw = "Advice FAST Payment / Receipt PAYNOW TRANSFER 1234567 TO: A N OTHER"
        name = normalise_counterparty(raw)
        assert not is_personal(name), "if this fails the leak closed some other way"
        assert propose([(name, -2500, raw)] * 5) == []

    def test_one_such_row_settles_the_payee(self):
        """A payee reached that way is a person whatever else they were paid
        by, so a single transfer is enough to withdraw the name."""
        rows = [("A N OTHER", -2500, "NETS PURCHASE A N OTHER")] * 4
        rows += [("A N OTHER", -2500, "PAYNOW TRANSFER 1234567 TO: A N OTHER")]
        assert propose(rows) == []

    def test_a_bare_account_number_is_never_proposed(self):
        assert propose([("ADVICE FUNDS TRANSFER 01-2345678-9", -20000)] * 3) == []

    def test_a_conduit_is_not_worth_asking_about(self):
        assert propose([("LAZADA SINGAPORE PAYM", -5000)] * 4) == []

    def test_a_merchant_is_proposed(self):
        result = propose([("SHENG SIONG", -2000)] * 3)
        assert len(result) == 1 and result[0].counterparty == "SHENG SIONG"
        assert result[0].occurrences == 3

    def test_nothing_carries_a_date_or_a_row(self):
        """The task needs a vocabulary, not a ledger.

        Asserted as the absence of the dangerous shapes rather than an exact
        field list, so adding something harmless — a suggestion to confirm —
        does not fail, while adding a date does.
        """
        fields = set(Proposal.__slots__)
        forbidden = ("date", "time", "txn", "account", "id", "member", "balance", "raw")
        assert not [f for f in fields if any(word in f for word in forbidden)]

    def test_what_a_rule_already_decides_is_not_asked_about(self):
        rows = [("SHENG SIONG", -2000)] * 3 + [("NEW SHOP", -900)] * 2
        result = propose(rows, [Rule(r"sheng siong", "Grocery")])
        assert [p.counterparty for p in result] == ["NEW SHOP"]

    def test_the_order_is_the_money_at_stake(self):
        """Ranking by frequency optimises the row count and ignores the large
        one-off spends, which is where the money actually goes: on the real
        corpus the top sixty by value held 84% of everything unaccounted for,
        while ordering by count kept offering another bus fare."""
        rows = [("OFTEN", -100)] * 9 + [("COSTLY", -50_000)] * 2
        assert [p.counterparty for p in propose(rows)] == ["COSTLY", "OFTEN"]
        assert [p.counterparty for p in propose(rows, by="occurrences")] == ["OFTEN", "COSTLY"]

    def test_the_real_total_never_leaves(self):
        """It orders the list and nothing else: a gated magnitude may go out,
        a true sum may not."""
        result = propose([("SHOP", -12_345)] * 3)[0]
        assert result.total_value_minor == 37_035
        assert result.typical_amount_minor == gate_amount(12_345)

    def test_an_outlier_does_not_describe_a_merchant(self):
        """The median, so one unusual purchase does not set the magnitude."""
        rows = [("IKEA", -350)] * 5 + [("IKEA", -89000)]
        assert propose(rows)[0].typical_amount_minor == gate_amount(350)


class TestTheSampleLine:
    """A counterparty is sometimes unanswerable on its own.

    `PACS BNC-P/` is what DBS calls an insurance premium, and a model asked to
    categorise that name is exactly as stuck as the person reading it. The line
    underneath says who was paid — so one line may go, stripped of everything
    that says *which customer* rather than *what merchant*.
    """

    GIRO = "GIRO Payments / Collections via GIRO ACME LIFE PACS BNC-P/N12345678"

    def test_the_line_that_names_the_payee_travels(self):
        result = propose([("PACS BNC-P/", -20000, self.GIRO)] * 4)[0]
        assert "ACME LIFE" in result.sample_description

    def test_every_number_is_masked(self):
        """A policy number identifies a customer and describes no merchant."""
        sample = redact_description(self.GIRO)
        assert "12345678" not in sample and not any(c.isdigit() for c in sample)

    def test_the_length_of_a_reference_does_not_survive_either(self):
        """One mark per run, not per digit: `####` and `########` would say
        which scheme it was even with the digits gone."""
        assert redact_description("ACME REF 12") == redact_description("ACME REF 1234") == "ACME REF #"

    def test_a_bare_account_number_refuses_the_whole_line(self):
        """Masking it would be enough for privacy, and refusing is still
        right: a line whose only distinguishing content is an account number
        describes no merchant, so there is nothing to weigh against the risk
        of having judged that wrong."""
        assert redact_description("ADVICE TRANSFER 01-2345678-9") == ""

    def test_a_payment_between_people_sends_no_line_at_all(self):
        """The text after `TO:` is whatever the payer typed, and on this corpus
        that is sometimes a first name. Nothing tells a payee's name from a
        shop's reliably, so these are refused wholesale rather than filtered."""
        assert redact_description("FAST PAYMENT PAYNOW TRANSFER 123456 TO: A N OTHER") == ""
        assert redact_description("ADVICE FUNDS TRANSFER FROM: A N OTHER") == ""

    def test_a_line_that_only_repeats_the_name_is_not_sent(self):
        """Padding every proposal with an echo of its own counterparty costs
        context and tells a model nothing."""
        assert propose([("SHENG SIONG", -2000, "Sheng Siong")] * 3)[0].sample_description == ""

    def test_nor_is_the_name_with_the_bank_s_tail_on_it(self):
        """`GUARDIAN PHARMACY CARD PAYMENT` has said everything the name
        says. On the real ledger this was 245 of 286 proposals."""
        rows = [("GUARDIAN PHARMACY", -2000, "GUARDIAN PHARMACY CARD PAYMENT")] * 3
        assert propose(rows)[0].sample_description == ""

    def test_punctuation_normalisation_dropped_is_not_a_difference(self):
        """`FISH & CO` normalises to `FISH CO`, and shipping the whole line to
        restore an ampersand is not worth the context."""
        rows = [("FISH CO", -2000, "FISH & CO")] * 3
        assert propose(rows)[0].sample_description == ""

    def test_a_mechanism_printed_before_the_payee_is_worth_sending(self):
        """The case the name cannot express. `ACME LIFE PACS BNC-P/` does not
        say that this is a standing arrangement; the words in front of it do,
        and normalisation drops them because they are not the payee."""
        rows = [("ACME LIFE PACS BNC-P/", -20000, self.GIRO)] * 4
        assert propose(rows)[0].sample_description.startswith("GIRO PAYMENTS")

    def test_the_commonest_line_stands_for_the_merchant(self):
        rows = [("ACME", -100, "BILLED MONTHLY ACME")] * 3 + [("ACME", -100, "ONE OFF ACME")]
        assert propose(rows)[0].sample_description == "BILLED MONTHLY ACME"

    def test_rows_without_a_line_still_work(self):
        """The pairs the rest of the codebase passes must keep working."""
        assert propose([("SHENG SIONG", -2000)] * 3)[0].sample_description == ""

    def test_nothing_a_ledger_line_would_carry_fits(self):
        assert redact_description("X" * 400) == "X" * SAMPLE_LIMIT


class TestWritingToTheLedger:
    """A correction that an automated pass could undo is not a correction."""

    def _seed(self, repository, context):
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        account = AccountRecord(
            institution="Test", account_ref_masked="acct", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        document = DocumentRecord(
            sha256="e" * 64, institution="Test", doc_type="acc",
            period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
            storage_path="x", parse_status="imported",
            source_profile="dummy", source_relpath="a.pdf",
            fetched_at=datetime.now(timezone.utc),
        )
        txns = [
            TxnRecord(
                account_key=account, posted_date=date(2026, 6, 3), amount_minor=-100 * n,
                currency="SGD", description_raw="t", description_norm="t",
                counterparty_norm="SHOP", dedupe_key=f"k{n}", seq=0,
            )
            for n in (1, 2)
        ]
        repository.insert_document(context, document, [], txns)

    def test_a_rule_pass_is_replaced_not_appended(self, repository, context):
        """Rerunning must give the same answer, and appending would leave a
        transaction wearing two categories with nothing to say which is now."""
        self._seed(repository, context)
        decided = [(1, "Grocery", 1.0), (2, "Grocery", 1.0)]

        first = repository.replace_rule_enrichments(context, decided)
        second = repository.replace_rule_enrichments(context, decided)

        assert first["written"] == 2 and first["replaced"] == 0
        assert second["written"] == 2 and second["replaced"] == 2
        assert sum(r["rows"] for r in repository.category_totals(context)) == 2

    def test_a_human_decision_is_never_overwritten(self, repository, context):
        """The whole value of correcting something is that it stays
        corrected."""
        from datetime import datetime, timezone

        from app.storage import schema

        self._seed(repository, context)
        with repository.engine.begin() as conn:
            conn.execute(schema.txn_enrichment.insert(), [{
                "tenant_id": context.tenant_id, "txn_id": 1,
                "category": "Dining", "confidence": 1.0, "source": "human",
                "computed_at": datetime.now(timezone.utc),
            }])

        result = repository.replace_rule_enrichments(
            context, [(1, "Grocery", 1.0), (2, "Grocery", 1.0)]
        )

        assert result["left_to_humans"] == 1
        # The rule pass declined to speak about that transaction at all.
        assert result["written"] == 1
        totals = {r["category"]: r["rows"] for r in repository.category_totals(context)}
        assert totals == {"Dining": 1, "Grocery": 1}


class TestReviewQueue:
    """What is left for a person, and what deciding it is worth."""

    def _queue(self, rows, rules=()):
        from app.domain.categories import review_queue

        return review_queue(rows, rules)

    def test_ranked_by_value_not_frequency(self):
        """The largest sixty unplaced names carried 88% of the unaccounted
        money on the real corpus; ranking by count offers bus fares first."""
        rows = [("OFTEN", -200)] * 20 + [("COSTLY", -90_000)] * 2
        assert [i.counterparty for i in self._queue(rows)] == ["COSTLY", "OFTEN"]

    def test_what_a_rule_settles_is_not_in_the_queue(self):
        rows = [("SHENG SIONG", -2000)] * 3 + [("MYSTERY", -5000)] * 2
        queue = self._queue(rows, [Rule(r"sheng siong", "Grocery")])
        assert [i.counterparty for i in queue] == ["MYSTERY"]

    def test_conduits_and_people_are_not_anyone_s_to_classify(self):
        rows = [("PAYLAH TOP UP", -1000)] * 3 + [("FAST PAYMENT FROM: A N OTHER", -1000)] * 3
        assert self._queue(rows) == []

    def test_it_carries_what_the_decision_is_worth(self):
        rows = [("SHOP", -1500)] * 4
        item = self._queue(rows)[0]
        assert (item.occurrences, item.total_minor) == (4, 6000)


class TestOperatorDecisions:
    def test_a_decision_is_anchored_to_the_name_it_was_about(self):
        """A loose pattern would claim every other name containing it."""
        from app.domain.categories import operator_rule

        pattern, category, weight, _ = operator_rule("SHOP", "Grocery")
        rule = Rule(pattern=pattern, category=category, weight=weight)

        assert rule.matches("SHOP")
        assert not rule.matches("SHOPEE SINGAPORE")

    def test_a_decision_outranks_anything_imported(self):
        """Having decided something by hand is the end of the argument, not
        another vote in it."""
        from app.domain.categories import operator_rule

        pattern, category, weight, _ = operator_rule("IKEA", "Business services")
        decided = Rule(pattern=pattern, category=category, weight=weight)
        shipped = Rule(r"ikea", "Furnishing")

        assert categorise("IKEA", [shipped, decided]).category == "Business services"

    def test_a_regex_in_a_merchant_name_is_not_a_pattern(self):
        """Names carry brackets and dots; escaping is what stops one becoming
        a wildcard."""
        from app.domain.categories import operator_rule

        pattern, category, weight, _ = operator_rule("LAZADA (PAYM", "Others")
        rule = Rule(pattern=pattern, category=category, weight=weight)
        assert rule.matches("LAZADA (PAYM")


class TestSeedRules:
    """The rules that ship, and what they are allowed to decide."""

    def _seeds(self):
        from app.domain.seed_rules import seed_rules

        return seed_rules()

    def test_every_seed_names_a_real_category(self):
        """A rule pointing at a category nobody has is a rule that never
        fires and never says so."""
        for rule in self._seeds():
            assert rule.category in DEFAULT_CATEGORIES, rule.pattern

    def test_every_seed_pattern_compiles(self):
        assert self._seeds()  # Rule.__post_init__ raises on a bad pattern

    def test_the_restaurant_beats_the_furniture_shop(self):
        """The case already sitting in this corpus, decided by the shipped
        rules rather than by anything the operator has to write."""
        seeds = self._seeds()
        assert categorise("IKEA-RESTAURANT", seeds).category == "Dining"
        assert categorise("IKEA TAMPINES", seeds).category == "Furnishing"

    def test_a_suggestion_travels_with_its_evidence(self):
        """Sent as a claim to be confirmed, not applied silently, so the
        pattern that matched goes with it."""
        from app.domain.categories import propose

        result = propose([("SHENG SIONG BEDOK", -2000)] * 3, suggest=self._seeds())
        assert result[0].suggested_category == "Grocery"
        assert result[0].suggested_by

    def test_a_seed_does_not_settle_the_question(self):
        """A seeded guess is right often enough to send and wrong often enough
        that it must not close the question."""
        from app.domain.categories import propose

        result = propose([("SHENG SIONG", -2000)] * 3, suggest=self._seeds())
        assert len(result) == 1, "still asked about, despite having a suggestion"

    def test_a_settled_rule_does_close_it(self):
        from app.domain.categories import propose

        result = propose(
            [("SHENG SIONG", -2000)] * 3,
            rules=[Rule(r"sheng siong", "Grocery")],
            suggest=self._seeds(),
        )
        assert result == []


class TestConsolidation:
    """Several models, and what their agreement is worth."""

    def test_unanimity_is_the_signal(self):
        verdict = consolidate({
            "a": {"NTUC": "Grocery"},
            "b": {"NTUC": "Grocery"},
        })[0]
        assert verdict.category == "Grocery"
        assert verdict.is_unanimous and not verdict.needs_review

    def test_a_split_goes_to_review_with_its_dissent(self):
        """Recorded rather than resolved: which model said what is the thing
        worth reading."""
        verdict = consolidate({
            "a": {"GRAB": "Transport"},
            "b": {"GRAB": "Transport"},
            "c": {"GRAB": "Dining"},
        })[0]
        assert verdict.needs_review
        assert (verdict.agreed, verdict.answered) == (2, 3)
        assert verdict.dissent == (("Dining", "c"),)

    def test_silence_is_not_dissent(self):
        """Replies are routinely partial — one model returned 177 of 500 —
        and counting a skip as disagreement would make a short answer look
        like a dispute."""
        verdict = consolidate({
            "a": {"NTUC": "Grocery"},
            "b": {"OTHER SHOP": "Dining"},
        })
        ntuc = next(v for v in verdict if v.counterparty == "NTUC")
        assert ntuc.is_unanimous and ntuc.answered == 1

    def test_an_invented_category_is_discarded(self):
        """A model answering outside the set is answering a different
        question."""
        verdicts = consolidate({
            "a": {"X": "Groceries and Food"},
            "b": {"X": "Grocery"},
        })
        assert verdicts[0].category == "Grocery" and verdicts[0].answered == 1

    def test_agreement_is_not_averaged_confidence(self):
        """Averaging would let one assured model outweigh two doubtful ones
        that agree, which discards the only real evidence available."""
        verdict = consolidate({
            "sure": {"X": "Dining"},
            "a": {"X": "Grocery"},
            "b": {"X": "Grocery"},
        })[0]
        assert verdict.category == "Grocery" and verdict.agreed == 2


class TestAmountGates:
    def test_an_amount_carries_a_magnitude_not_a_purchase(self):
        assert gate_amount(-347) == 500
        assert gate_amount(-89000) == 100_000

    def test_a_tie_rounds_up(self):
        """Never understating is the safer direction for a coarse figure."""
        assert gate_amount(300) == 500

    def test_the_gates_disambiguate_the_case_in_this_corpus(self):
        """IKEA at 3.50 is Dining; IKEA at 890 is Furnishing. The magnitude is
        what a model needs, and all it needs."""
        assert gate_amount(350) != gate_amount(89000)


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
