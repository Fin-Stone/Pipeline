"""Adapter routing.

Exact fingerprint match, and nothing else. There is no sniffing, no scoring and
no best-effort fallback: an unrecognised layout quarantines with its computed
fingerprint recorded, because silent corruption of the ledger is far worse than
a failed import. A failed import is visible today; a corrupted ledger is
discovered years later.

Many fingerprints may map to one adapter — issuers reflow their headers between
years without changing anything an adapter cares about — so registering a
variant is a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.parser import StatementAdapter


class UnknownLayout(Exception):
    """No adapter is registered for this fingerprint."""

    def __init__(self, fingerprint: str, detail: dict | None = None):
        super().__init__(f"no adapter registered for layout fingerprint {fingerprint}")
        self.fingerprint = fingerprint
        self.detail = detail or {}


@dataclass
class AdapterRegistry:
    _by_fingerprint: dict[str, StatementAdapter]

    def __init__(self) -> None:
        self._by_fingerprint = {}

    def register(self, adapter: StatementAdapter, fingerprints: list[str]) -> None:
        for fingerprint in fingerprints:
            existing = self._by_fingerprint.get(fingerprint)
            if existing is not None and existing is not adapter:
                raise ValueError(
                    f"fingerprint {fingerprint} is already registered to {existing.name}; "
                    "two adapters cannot claim the same layout"
                )
            self._by_fingerprint[fingerprint] = adapter

    def resolve(self, fingerprint: str) -> StatementAdapter:
        adapter = self._by_fingerprint.get(fingerprint)
        if adapter is None:
            raise UnknownLayout(fingerprint)
        return adapter

    def known_fingerprints(self) -> dict[str, str]:
        return {fp: adapter.name for fp, adapter in sorted(self._by_fingerprint.items())}

    def __len__(self) -> int:
        return len(self._by_fingerprint)


def build_default_registry() -> AdapterRegistry:
    """The registry the pipeline runs with.

    Adding an institution means importing its adapter here and listing the
    fingerprints observed for it. Nothing else in the pipeline changes.
    """
    from .trust.acc import TrustAccountAdapter, FINGERPRINTS as TRUST_ACC_FINGERPRINTS
    from .trust.cc import TrustCardAdapter, FINGERPRINTS as TRUST_CC_FINGERPRINTS

    registry = AdapterRegistry()
    registry.register(TrustAccountAdapter(), TRUST_ACC_FINGERPRINTS)
    registry.register(TrustCardAdapter(), TRUST_CC_FINGERPRINTS)
    return registry
