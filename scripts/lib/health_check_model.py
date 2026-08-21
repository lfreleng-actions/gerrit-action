# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Value types and tuning constants shared by the health checks.

Split out of :mod:`health_check` so the probe and flow modules can
share the retry policy, the log patterns Gerrit output is matched
against, and the per-instance result record without importing each
other.  Every name here is re-exported from :mod:`health_check`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


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

# Pattern that Gerrit logs when it has finished starting up
_READY_PATTERN = re.compile(r"Gerrit Code Review.*ready")

# Pattern that identifies replica/headless mode
_REPLICA_PATTERN = re.compile(r"\[replica\].*\[headless\]")

# HTTP status codes considered "healthy" (the endpoint exists and responds)
_HEALTHY_HTTP_CODES = {200, 401, 403}


# ---------------------------------------------------------------------------
# Per-instance result
# ---------------------------------------------------------------------------


@dataclass
class HealthCheckResult:
    """Result of a health check for a single instance."""

    slug: str
    success: bool = False
    error: str = ""
    is_replica: bool = False
