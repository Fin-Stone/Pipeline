"""Running the reconciliation check against a real ledger.

The rules live in `app.domain.reconcile` and know nothing about storage. This
is the seam that feeds them: two reads, one conversion, no arithmetic. Keeping
the arithmetic on the other side of it is what lets the awkward cases — a
statement that declares no balances, two statements closing on the same day —
be tested without a database.
"""

from __future__ import annotations

from ..domain.reconcile import Declaration, Drift, drifts


def account_label(row: dict) -> str:
    """What an operator calls this account, from what the statement printed.

    ASCII only, and `/` between the account and its pocket to match
    `validate._label`. This lands on a Windows console often enough that a
    prettier separator is a mojibake risk for nothing.
    """
    parts = [row.get("institution") or "", row.get("account_ref_masked") or ""]
    label = row.get("sub_account_label")
    name = " ".join(p for p in parts if p)
    return f"{name}/{label}" if label else name


def check(repository, context) -> list[Drift]:
    """Every disagreement between the declared balances and the ledger."""
    declarations = [
        Declaration(
            account_id=row["account_id"],
            account=account_label(row),
            period_start=row["period_start"],
            period_end=row["period_end"],
            opening_balance_minor=row["opening_balance_minor"],
            closing_balance_minor=row["closing_balance_minor"],
        )
        for row in repository.declared_balances(context)
    ]
    return drifts(declarations, repository.daily_movement(context))
