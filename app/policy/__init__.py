"""Policy engine — pluggable pre-flight checks for the OpenCode Gateway.

The policy engine provides a Protocol-based interface that allows
different check strategies (observation-based, resource-quota, etc.)
to be plugged into the job-acceptance flow without coupling the
Gateway to any specific implementation.
"""

from __future__ import annotations

from app.policy.base import PolicyViolation, PreflightPolicy
from app.policy.observation import ObservationBasedPolicy

__all__ = ["ObservationBasedPolicy", "PolicyViolation", "PreflightPolicy"]
