"""The alerting seam.

Phase 1 logs. Later phases route to ntfy or Gotify (architecture §8.2) without
touching pipeline code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, message: str, *, severity: str = SEVERITY_INFO, **fields) -> None:
        """Deliver an operator-facing notification.

        Must never raise: a failure to notify cannot be allowed to fail the
        run that was trying to report a problem.
        """
