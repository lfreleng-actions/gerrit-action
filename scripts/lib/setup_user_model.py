# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Value types and constants shared by the user setup steps.

Split out of ``setup-gerrit-user.py`` so the input, instance and
summary modules can share the username rules, the per-instance retry
policy and the outcome record without importing each other.  Every
name here is re-exported from ``setup-gerrit-user.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Username validation
# ---------------------------------------------------------------------------

# Username validation: only safe characters to prevent command injection
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_USERNAME_MAX_LEN = 64


# ---------------------------------------------------------------------------
# Per-instance retry policy
# ---------------------------------------------------------------------------

# The Gerrit container may still be initialising its auth subsystem even
# though the health-check (which hits a public endpoint) already passed.
_MAX_ATTEMPTS = 3
_INITIAL_RETRY_DELAY = 3  # seconds

# Only transient failures are retried: network errors (no status code)
# or these HTTP codes.  401/403 are included because the Gerrit auth
# subsystem may not be fully ready immediately after the container
# passes its health check (which hits a public endpoint).
_TRANSIENT_STATUS = {401, 403, 429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Per-instance outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceOutcome:
    """Result of applying SSH keys to a single instance."""

    kind: str  # "configured", "skipped" or "failed"
    status: str  # label used in the multi-instance summary table


CONFIGURED = InstanceOutcome("configured", "✅ Configured")
SKIPPED_NO_CONTAINER = InstanceOutcome("skipped", "⚠️ Skipped (no container)")
FAILED = InstanceOutcome("failed", "❌ Failed")
