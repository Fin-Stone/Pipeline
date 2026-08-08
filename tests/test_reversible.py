"""Every action a person can take has a way back.

Not a nice-to-have. Each of these decisions moves money out of the totals on a
single click, from a dense list where the buttons sit inches apart, and two of
them used to leave no trace at all: the row vanished, the figures changed, and
nothing anywhere could name it again.

The test that matters in each case is the same one — do it, undo it, and check
the ledger is *exactly* as it was, not merely close.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.domain.models import DEPOSIT
from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

SHA = "e" * 64


def _seed(repository, context):
    account = AccountRecord(
        institution="Test", account_ref_masked="1", sub_account_label="",
        currency="SGD", kind=DEPOSIT,
    )
    repository.insert_document(
        context,
        DocumentRecord(
            sha256=SHA, institution="Test", doc_type="acc",
            period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
            storage_path="x", parse_status="imported",
            source_profile="dummy", source_relpath="a.pdf",
            fetched_at=datetime.now(timezone.utc),
        ),
        [],
        [
            TxnRecord(
                account_key=account, posted_date=date(2026, 6, 3 + i), amount_minor=amount,
                currency="SGD", description_raw=name, description_norm=name,
                counterparty_norm=name, dedupe_key=f"r{i}", seq=i,
            )
            for i, (amount, name) in enumerate([
                (-300_00, "DINNER"), (60_00, "PAYNOW A"), (-50_00, "COFFEE"),
            ])
        ],
    )


def _figures(repository, context):
    """Everything a person sees, as one comparable value."""
    return {
        d: sorted(
            # `category` is None for anything nothing has filed yet, and None
            # does not sort against a string.
            (r["category"] or "", r["rows"], r["total_minor"])
            for r in repository.spending_summary(context, direction=d)
        )
        for d in ("out", "in", "net")
    }


@pytest.fixture
def ledger(repository, context):
    _seed(repository, context)
    return {"dinner": 1, "paynow": 2, "coffee": 3}


class TestEveryExclusionComesBack:
    def test_hiding(self, repository, context, ledger):
        before = _figures(repository, context)
        repository.hide_txn(context, ledger["coffee"], note="one-off")
        assert _figures(repository, context) != before

        assert repository.unhide_txn(context, ledger["coffee"]) is True
        assert _figures(repository, context) == before

    def test_marking_a_transfer(self, repository, context, ledger):
        """The one that had no way back at all. Clicking `transfer` removed the
        row from every figure, and no screen in the client could list it again,
        let alone undo it."""
        before = _figures(repository, context)
        repository.mark_transfer(context, ledger["coffee"])
        assert _figures(repository, context) != before

        assert repository.unmark_transfer(context, ledger["coffee"]) is True
        assert _figures(repository, context) == before

    def test_linking_a_payback(self, repository, context, ledger):
        before = _figures(repository, context)
        repository.link_paybacks(context, ledger["dinner"], [ledger["paynow"]])
        assert _figures(repository, context) != before

        assert repository.unlink_paybacks(context, ledger["dinner"]) == 1
        assert _figures(repository, context) == before

    def test_categorising(self, repository, context, ledger):
        before = _figures(repository, context)
        repository.set_human_category(context, ledger["coffee"], "Dining")
        assert _figures(repository, context) != before

        # Back to uncategorised, which is a state the dropdown has to be able
        # to return to or a correction can be made but never taken back.
        repository.clear_human_category(context, ledger["coffee"])
        assert _figures(repository, context) == before

    def test_clearing_every_correction_at_once(self, repository, context, ledger):
        """The bulk revert, for an operator who has moved to maintaining rules.

        A row-level correction beats the rules by design — that is the whole
        point of one — so while any survive, the rules cannot be seen. Listed
        before it deletes, because doing forty-five at once has no inverse and
        a record the operator can act on is the honest substitute.
        """
        repository.set_human_category(context, ledger["coffee"], "Dining")
        repository.set_human_category(context, ledger["dinner"], "Dining")

        listed = repository.list_human_categories(context)
        assert {row["id"] for row in listed} == {ledger["coffee"], ledger["dinner"]}
        assert all(row["category"] == "Dining" for row in listed)

        assert repository.clear_human_categories(context) == 2
        assert repository.list_human_categories(context) == []

    def test_clearing_leaves_what_the_rules_decided(self, repository, context, ledger):
        """Only corrections go. A rule pass is regenerable and is not the
        operator's own work in the way a correction is."""
        repository.replace_rule_enrichments(context, [(ledger["coffee"], "Grocery", 1.0)])
        repository.set_human_category(context, ledger["dinner"], "Dining")

        assert repository.clear_human_categories(context) == 1
        assert {row["category"] for row in repository.category_totals(context)} == {"Grocery"}


class TestAMarkCanBeFound:
    """Undo is only real if the thing to undo can be named later.

    A snackbar covers the misclick. It does not cover noticing next week that
    the totals look wrong, which is when a list is the only thing that helps.
    """

    def test_a_manual_mark_is_listed(self, repository, context, ledger):
        repository.mark_transfer(context, ledger["coffee"])
        marked = repository.list_manual_transfers(context)
        assert [m["out_txn_id"] for m in marked] == [ledger["coffee"]]
        assert marked[0]["txn_amount_minor"] == -50_00
        assert marked[0]["counterparty_norm"] == "COFFEE"

    def test_the_matchers_own_links_are_not_listed(self, repository, context, ledger):
        """They are regenerable and not anybody's decision, so offering to undo
        one would promise something a re-run immediately takes back."""
        from app.domain.transfers import Link

        repository.replace_transfer_links(context, [
            Link(out_txn_id=ledger["dinner"], in_txn_id=ledger["paynow"],
                 amount_minor=30000, days_apart=1, evidence="amount and date"),
        ])
        assert repository.list_manual_transfers(context) == []
        assert repository.count_transfer_links(context) == 1

    def test_a_payback_is_listed_without_naming_its_charge(self, repository, context, ledger):
        repository.link_paybacks(context, ledger["dinner"], [ledger["paynow"]])
        links = repository.list_paybacks(context)
        assert len(links) == 1
        assert links[0]["expense_txn_id"] == ledger["dinner"]


class TestAddingACategoryCanBeUndone:
    def test_an_unused_category_can_go(self, repository, context, ledger):
        """Which is what makes adding one safe to try."""
        repository.add_category(context, "Experiment")
        assert repository.delete_category(context, "Experiment") is True
        assert "Experiment" not in {c["name"] for c in repository.list_categories(context)}

    def test_one_in_use_is_refused_rather_than_cascading(self, repository, context, ledger):
        """Deleting a category rows are filed under would orphan them or
        silently re-file them, and neither is something a person can undo. So
        the destructive direction is the one that gets refused."""
        repository.add_category(context, "Experiment")
        repository.set_human_category(context, ledger["coffee"], "Experiment")

        with pytest.raises(ValueError, match="in use"):
            repository.delete_category(context, "Experiment")
        assert "Experiment" in {c["name"] for c in repository.list_categories(context)}

    def test_the_refusal_says_how_much_there_is_to_refile(self, repository, context, ledger):
        repository.add_category(context, "Experiment")
        repository.set_human_category(context, ledger["coffee"], "Experiment")
        with pytest.raises(ValueError, match="1 transaction"):
            repository.delete_category(context, "Experiment")

    def test_deleting_one_that_never_existed_is_not_an_error(self, repository, context):
        assert repository.delete_category(context, "Nothing") is False

    def test_usage_can_be_asked_before_trying(self, repository, context, ledger):
        repository.add_category(context, "Experiment")
        repository.set_human_category(context, ledger["coffee"], "Experiment")
        usage = repository.category_usage(context, "Experiment")
        assert usage == {"exists": True, "rules": 0, "transactions": 1}


class TestADecisionCanBeTakenBack:
    """Deciding a counterparty writes a rule that outranks everything imported
    and settles the name for good. That is a lot of consequence for one click,
    and until now nothing could name the rule afterwards, let alone remove it.
    """

    def _decide(self, repository, context, counterparty, category="Dining"):
        from app.domain.categories import operator_rule

        if category not in {c["name"] for c in repository.list_categories(context)}:
            repository.add_category(context, category)
        repository.add_category_rules(context, [operator_rule(counterparty, category)])
        return next(
            r for r in repository.list_category_rules(context)
            if r["pattern"] == rf"^{counterparty}$"
        )

    def test_the_rules_go_back_to_what_they_were(self, repository, context, ledger):
        before = repository.list_category_rules(context)
        rule = self._decide(repository, context, "COFFEE")
        assert repository.list_category_rules(context) != before

        assert repository.delete_category_rule(context, rule["id"]) is True
        assert repository.list_category_rules(context) == before

    def test_removing_one_twice_is_not_an_error(self, repository, context, ledger):
        rule = self._decide(repository, context, "COFFEE")
        assert repository.delete_category_rule(context, rule["id"]) is True
        assert repository.delete_category_rule(context, rule["id"]) is False

    def test_deciding_again_after_removing_works(self, repository, context, ledger):
        """The uniqueness constraint must not have kept the name spoken for."""
        from app.domain.categories import operator_rule

        rule = self._decide(repository, context, "COFFEE")
        repository.delete_category_rule(context, rule["id"])
        assert repository.add_category_rules(
            context, [operator_rule("COFFEE", "Dining")]
        ) == 1

    def test_removing_one_leaves_the_others_alone(self, repository, context, ledger):
        doomed = self._decide(repository, context, "COFFEE")
        kept = self._decide(repository, context, "DINNER")
        repository.delete_category_rule(context, doomed["id"])
        assert [r["id"] for r in repository.list_category_rules(context)] == [kept["id"]]

    def test_a_decision_can_be_told_apart_from_an_import(self, repository, context, ledger):
        """Only a decision is anybody's to take back. An imported rule was
        nobody's, and offering to undo one would promise something the next
        import takes straight back."""
        from app.domain.categories import rule_origin

        self._decide(repository, context, "COFFEE")
        repository.add_category_rules(
            context, [(r"^DINNER$", "Dining", 0, "agreed by 3/3 models")]
        )
        origins = {
            r["pattern"]: rule_origin(r["weight"], r["note"])
            for r in repository.list_category_rules(context)
        }
        assert origins == {r"^COFFEE$": "operator", r"^DINNER$": "imported"}

    def test_how_much_a_decision_covers_can_be_asked(self, repository, context, ledger):
        counts = repository.counterparty_row_counts(context, ["COFFEE", "DINNER", "NOTHING"])
        assert counts == {"COFFEE": 1, "DINNER": 1}

    def test_asking_about_nothing_costs_no_query(self, repository, context, ledger):
        assert repository.counterparty_row_counts(context, []) == {}


class TestUndoingIsIdempotent:
    """A double-click on Undo must not do something different from one click."""

    def test_unhiding_twice(self, repository, context, ledger):
        repository.hide_txn(context, ledger["coffee"])
        assert repository.unhide_txn(context, ledger["coffee"]) is True
        assert repository.unhide_txn(context, ledger["coffee"]) is False

    def test_unmarking_twice(self, repository, context, ledger):
        repository.mark_transfer(context, ledger["coffee"])
        assert repository.unmark_transfer(context, ledger["coffee"]) is True
        assert repository.unmark_transfer(context, ledger["coffee"]) is False

    def test_unlinking_twice(self, repository, context, ledger):
        repository.link_paybacks(context, ledger["dinner"], [ledger["paynow"]])
        assert repository.unlink_paybacks(context, ledger["dinner"]) == 1
        assert repository.unlink_paybacks(context, ledger["dinner"]) == 0

    def test_relinking_after_an_undo(self, repository, context, ledger):
        """Undo then redo. The uniqueness constraint must not have kept the
        inflow spoken for after it was released."""
        repository.link_paybacks(context, ledger["dinner"], [ledger["paynow"]])
        repository.unlink_paybacks(context, ledger["dinner"])
        repository.link_paybacks(context, ledger["dinner"], [ledger["paynow"]])
        assert len(repository.list_paybacks(context, ledger["dinner"])) == 1
