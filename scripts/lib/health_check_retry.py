# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Retry and timeout policy shared by the Gerrit health checks.

Every health-check probe waits on a resource that only becomes
available some time after a container starts, so each one needs a
retry budget.  Those budgets live here rather than beside the probes
because :mod:`health_check` and :mod:`health_check_container` both
consume them: keeping them in a leaf module lets the probe modules
share the policy without importing one another.

The values are also the tuning surface for slow CI runners — a
reviewer looking for "how long will this wait before failing?" should
only ever have to read this file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryConfig:
    """Retry parameters for health checks."""

    max_retries: int = 30
    interval: float = 2.0
    timeout: float = 0.0  # 0 means no overall timeout (use max_retries × interval)

    @property
    def effective_timeout(self) -> float:
        """Total wall-clock seconds this retry config could consume."""
        if self.timeout > 0:
            return self.timeout
        return self.max_retries * self.interval


# Sensible defaults
GERRIT_READY_TIMEOUT = 180  # seconds to wait for "Gerrit Code Review … ready"
HTTP_RETRY = RetryConfig(max_retries=30, interval=2.0)
TCP_RETRY = RetryConfig(max_retries=30, interval=2.0)
TCP_SSH_RETRY = RetryConfig(max_retries=15, interval=2.0)
