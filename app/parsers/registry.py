"""Adapter routing.

An adapter declares a `LayoutSignature` — the header lines that identify its
format — and a document routes to it when it contains all of them. Nothing
else. There is no scoring, no nearest match and no best-effort fallback:

- **No match quarantines.** Silent corruption of the ledger is far worse than a
  failed import. A failed import is visible today; a corrupted ledger is
  discovered years later.
- **More than one match is an error, not a coin toss.** If two adapters both
  claim a document, the signatures are wrong and that needs fixing, not
  papering over.

Subset matching rather than an exact hash because the exact version broke twice
in production for reasons unrelated to any layout — and because it made routing
depend on the customer's name and address. See `fingerprint.LayoutSignature`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ports.parser import StatementAdapter
from .fingerprint import LayoutSignature, fingerprint_pdf, label_lines
from .pdfio import Document


class LayoutError(Exception):
    """Base for routing failures. Always a quarantine, never a guess."""


class UnknownLayout(LayoutError):
    """No adapter claims this document."""

    def __init__(self, fingerprint: str, detail: dict | None = None):
        super().__init__(f"no adapter registered for layout fingerprint {fingerprint}")
        self.fingerprint = fingerprint
        self.detail = detail or {}


class AmbiguousLayout(LayoutError):
    """Several adapters claim this document, so none of them can be trusted."""

    def __init__(self, fingerprint: str, names: list[str]):
        super().__init__(
            f"{len(names)} adapters claim layout {fingerprint}: {', '.join(names)}. "
            "Their signatures are not distinct enough; make them so rather than "
            "letting one win arbitrarily."
        )
        self.fingerprint = fingerprint
        self.names = names


@dataclass
class Registration:
    adapter: StatementAdapter
    signature: LayoutSignature


@dataclass
class AdapterRegistry:
    _registrations: list[Registration] = field(default_factory=list)

    def register(self, adapter: StatementAdapter, signature: LayoutSignature) -> None:
        self._registrations.append(Registration(adapter, signature))

    def resolve(self, document: Document) -> StatementAdapter:
        matches = [r for r in self._registrations if r.signature.matches(document)]

        if len(matches) == 1:
            return matches[0].adapter

        fingerprint = fingerprint_pdf(document) if document.pages else "unknown"
        if not matches:
            raise UnknownLayout(fingerprint)
        raise AmbiguousLayout(fingerprint, [r.adapter.name for r in matches])

    def explain(self, document: Document) -> list[dict]:
        """Why each adapter did or did not claim a document.

        The useful half of a routing failure: not "no match" but *which
        required line was absent*.
        """
        return [
            {
                "adapter": r.adapter.name,
                "matches": r.signature.matches(document),
                "missing": r.signature.missing_from(document),
                "expects_producer": " ".join(r.signature.producer or r.signature.creator) or "(any)",
            }
            for r in self._registrations
        ]

    def registrations(self) -> list[Registration]:
        return list(self._registrations)

    def __len__(self) -> int:
        return len(self._registrations)


def build_default_registry() -> AdapterRegistry:
    """The registry the pipeline runs with.

    Adding an institution means importing its adapter here and declaring the
    header lines that identify it. Nothing else in the pipeline changes.
    """
    from .dbs.acc import SIGNATURE as DBS_ACC_SIGNATURE, DbsAccountAdapter
    from .trust.acc import SIGNATURE as TRUST_ACC_SIGNATURE, TrustAccountAdapter
    from .trust.cc import SIGNATURE as TRUST_CC_SIGNATURE, TrustCardAdapter

    registry = AdapterRegistry()
    registry.register(TrustAccountAdapter(), TRUST_ACC_SIGNATURE)
    registry.register(TrustCardAdapter(), TRUST_CC_SIGNATURE)
    registry.register(DbsAccountAdapter(), DBS_ACC_SIGNATURE)
    return registry
