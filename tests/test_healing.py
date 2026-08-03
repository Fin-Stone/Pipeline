"""Treating an unrecognised vendor string as a rename to be proven.

Institutions rename the tool that renders their statements. DBS's went
"Quadient Group AG~Inspire" to "Quadient CXM AG~Inspire" to "Quadient~Inspire"
— 107 statements stopped routing over it. Trust's went "Skia/PDF m80" to
"m141". Each time every header line still matched and only the vendor moved.

So the pipeline proposes the adapter whose header lines match and *verifies* it
by requiring the statement to reconcile to the cent. The tests that matter here
are the refusals: healing must be impossible wherever the oracle is absent or
ambiguous, because that is where a wrong adapter could pass unnoticed.
"""

from __future__ import annotations

import json

import pytest

from app.parsers.fingerprint import LayoutSignature
from app.parsers.learned import LearnedRules
from app.parsers.registry import AdapterRegistry, UnknownLayout
from app.pipeline.ingest import ingest_inbox

from .fixtures.make_pdf import SYNTHETIC_REQUIRES, synthetic_statement, write_pdf
from .test_pipeline import SyntheticAdapter

ROWS = [("03 Jun", "Salary", "+2,000.00")]

#: The fixtures carry no PDF metadata at all, so an adapter demanding a vendor
#: never matches by signature — the situation a rename produces.
DEMANDS_A_VENDOR = LayoutSignature(
    producer=("acme", "renderer"), requires=SYNTHETIC_REQUIRES,
)


def _registry(config, *adapters):
    registry = AdapterRegistry(learned=LearnedRules.load(config.learned_rules_path))
    for adapter in adapters or (SyntheticAdapter(),):
        registry.register(adapter, DEMANDS_A_VENDOR)
    return registry


def _drop(config, name="statement.pdf", rows=None, **kwargs):
    return write_pdf(
        config.inbox_dir / "dummy" / name,
        synthetic_statement(rows if rows is not None else ROWS, opening="1,000.00", **kwargs),
    )


class TestHealing:
    def test_a_renamed_vendor_is_accepted_when_the_statement_reconciles(
        self, config, repository, context, blob_store, notifier
    ):
        _drop(config, closing="3,000.00")
        summary = ingest_inbox(config, context, repository, blob_store, notifier,
                               _registry(config))

        assert summary.imported == 1
        assert summary.healed == 1
        assert repository.counts(context).txns == 1

    def test_what_was_proven_is_recorded_with_its_evidence(
        self, config, repository, context, blob_store, notifier
    ):
        """An automatic adaptation must be auditable, not taken on trust."""
        _drop(config, closing="3,000.00")
        ingest_inbox(config, context, repository, blob_store, notifier, _registry(config))

        payload = json.loads(config.learned_rules_path.read_text(encoding="utf-8"))
        vendor = payload["vendors"][0]
        assert vendor["adapter"] == "test.synthetic"
        assert vendor["evidence"]["accounts_reconciled"] == 1
        assert vendor["evidence"]["transactions"] == 1
        assert vendor["learned_at"]

    def test_the_next_statement_routes_without_healing(
        self, config, repository, context, blob_store, notifier
    ):
        """Learning is what stops it being a search every time."""
        _drop(config, "first.pdf", closing="3,000.00")
        ingest_inbox(config, context, repository, blob_store, notifier, _registry(config))

        # A different month, so it is a different statement rather than the
        # same one arriving twice.
        _drop(config, "second.pdf", closing="3,000.00",
              rows=[("03 Jul", "Salary", "+2,000.00")],
              period="1 Jul 2024 - 31 Jul 2024")
        second = ingest_inbox(config, context, repository, blob_store, notifier,
                              _registry(config))

        assert second.imported == 1
        assert second.healed == 0, "the vendor was already learned; no proving needed"

    def test_a_learned_rule_still_requires_the_header_lines(self, config):
        """Only the vendor string is relaxed. The lines that identify the
        format are what routing rests on and are never inferred, so a learned
        vendor does not make an adapter claim a document it never matched."""
        from app.parsers import pdfio
        from app.parsers.learned import LearnedVendor

        registry = _registry(config)
        registry.learned.vendors.append(
            LearnedVendor(adapter="test.synthetic", producer="", creator="")
        )

        # Same (absent) vendor metadata, but a header the adapter never claimed.
        stranger = _drop(config, "x.pdf", closing="3,000.00",
                         strapline="A COMPLETELY DIFFERENT BANK")
        with pytest.raises(UnknownLayout):
            registry.resolve(pdfio.load(stranger))

        # The learned rule does apply to a document whose header does match.
        familiar = _drop(config, "y.pdf", closing="3,000.00")
        assert registry.resolve(pdfio.load(familiar)).name == "test.synthetic"


class TestRefusals:
    """Where the oracle is absent or ambiguous, healing must be impossible."""

    def test_a_statement_without_balances_is_never_healed(
        self, config, repository, context, blob_store, notifier
    ):
        """Nothing could verify it, and that is exactly where a wrong adapter
        would pass unnoticed."""
        _drop(config, closing=None)
        summary = ingest_inbox(config, context, repository, blob_store, notifier,
                               _registry(config))

        assert summary.healed == 0
        assert summary.quarantined == 1
        assert not config.learned_rules_path.exists()

    def test_a_statement_that_does_not_reconcile_is_never_healed(
        self, config, repository, context, blob_store, notifier
    ):
        _drop(config, closing="9,999.00")
        summary = ingest_inbox(config, context, repository, blob_store, notifier,
                               _registry(config))

        assert summary.healed == 0 and summary.quarantined == 1
        assert repository.counts(context).txns == 0

    def test_two_adapters_that_both_reconcile_is_a_refusal(
        self, config, repository, context, blob_store, notifier
    ):
        """Never a tiebreak: two candidates means the signatures cannot
        distinguish the format, which is a fault to fix, not to resolve."""
        class Twin(SyntheticAdapter):
            name = "test.twin"

        _drop(config, closing="3,000.00")
        summary = ingest_inbox(config, context, repository, blob_store, notifier,
                               _registry(config, SyntheticAdapter(), Twin()))

        assert summary.healed == 0 and summary.quarantined == 1

    def test_nothing_is_healed_when_no_adapter_matches_the_header(
        self, config, repository, context, blob_store, notifier
    ):
        _drop(config, closing="3,000.00", strapline="A COMPLETELY DIFFERENT BANK")
        summary = ingest_inbox(config, context, repository, blob_store, notifier,
                               _registry(config))

        assert summary.healed == 0 and summary.quarantined == 1


class TestLearnedStore:
    def test_a_corrupt_store_does_not_stop_ingestion(self, config):
        config.learned_rules_path.parent.mkdir(parents=True, exist_ok=True)
        config.learned_rules_path.write_text("{ not json", encoding="utf-8")
        # It only costs the shortcut; healing rediscovers what it held.
        assert len(LearnedRules.load(config.learned_rules_path)) == 0

    def test_an_absent_store_is_empty_rather_than_an_error(self, config):
        assert len(LearnedRules.load(config.learned_rules_path)) == 0
