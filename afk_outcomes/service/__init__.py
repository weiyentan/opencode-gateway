"""Service layer for AFK Outcome Observability.

Thin orchestration seams that separate the pure-domain policy in
:mod:`afk_outcomes` (models, run-status policy, correlation) from
persistence orchestration.  Each module here delegates to a pure-domain
policy function and adds no policy of its own.

This subpackage deliberately imports nothing from the application package
(``app``) — it is pure domain, a boundary enforced mechanically by a test
under ``tests/``.
"""

from __future__ import annotations

from afk_outcomes.service.lifecycle import get_run_status

__all__ = ["get_run_status"]
