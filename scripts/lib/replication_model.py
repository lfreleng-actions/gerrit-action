# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result records and progress tracking for replication.

Split out of :mod:`replication`; holds the per-instance trigger and
verification results, the point-in-time progress snapshot the wait
loop compares against, the steady-state tracker built on it, and the
two records the wait loop carries across its poll cycles.  Every name
here is re-exported from :mod:`replication`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from replication_patterns import _STABILITY_WINDOW_SECONDS


@dataclass
class TriggerResult:
    """Result of a replication trigger for a single instance."""

    slug: str
    success: bool = False
    replication_started: bool = False
    error: str = ""
    repo_count: int = 0
    expected_count: int = 0


@dataclass
class VerificationResult:
    """Result of replication verification for a single instance."""

    slug: str
    success: bool = False
    error: str = ""
    repo_count: int = 0
    expected_count: int = 0
    completed_count: int = 0
    disk_usage: str = ""
    disk_usage_mb: int = 0


@dataclass
class ReplicationSnapshot:
    """Point-in-time snapshot of replication progress.

    Used by the wait loop to detect whether replication is still making
    progress or has reached a steady state (no new fetches, no disk
    growth, no new log entries).
    """

    timestamp: float = 0.0
    completed_count: int = 0
    disk_usage_kb: int = 0
    log_line_count: int = 0
    repo_count: int = 0

    def is_same_as(self, other: ReplicationSnapshot) -> bool:
        """Return *True* if all observable counters are unchanged."""
        return (
            self.completed_count == other.completed_count
            and self.disk_usage_kb == other.disk_usage_kb
            and self.log_line_count == other.log_line_count
            and self.repo_count == other.repo_count
        )


@dataclass
class _StabilityTracker:
    """Track how long replication state has been unchanging.

    The tracker records the timestamp at which the state last changed.
    Callers push new snapshots via :meth:`update` and query whether the
    state has been stable for at least *window* seconds.
    """

    window: float = _STABILITY_WINDOW_SECONDS
    _last_change_time: float = field(default=0.0, init=False)
    _prev_snapshot: ReplicationSnapshot | None = field(default=None, init=False)

    def update(self, snap: ReplicationSnapshot) -> None:
        """Record a new snapshot; reset the clock if state changed."""
        if self._prev_snapshot is None or not snap.is_same_as(self._prev_snapshot):
            self._last_change_time = snap.timestamp
        self._prev_snapshot = snap

    def is_stable(self, now: float) -> bool:
        """Return *True* if the state has not changed for *window* seconds."""
        if self._prev_snapshot is None:
            return False
        return (now - self._last_change_time) >= self.window

    @property
    def seconds_stable(self) -> float:
        """Seconds since the last state change (0 if no snapshot yet)."""
        if self._prev_snapshot is None:
            return 0.0
        return self._prev_snapshot.timestamp - self._last_change_time


@dataclass(frozen=True)
class _WaitSettings:
    """The arguments to one ``wait_for_replication`` call that never change.

    Bundled so the poll-cycle helpers can be handed the whole set
    instead of repeating eight positional arguments each.  ``project``
    is deliberately absent: it is only echoed in the preamble and is
    passed there directly.
    """

    slug: str
    timeout: int
    expected_count: int
    debug: bool
    stability_window: int


@dataclass
class _SeenMatches:
    """Error-match lines the wait loop has already reported.

    ``check_replication_errors`` rescans the same log on every poll,
    so without this the identical ``Cannot replicate from …
    All-Users.git`` line would be re-emitted roughly twelve times a
    minute and bury the progress output.  One set per heading, so a
    line reclassified between headings still gets reported under the
    new one.
    """

    advisory: set[str] = field(default_factory=set)
    soft_failure: set[str] = field(default_factory=set)
    magic_repo: set[str] = field(default_factory=set)
    user_project: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_int(raw: str) -> int:
    """Parse a string to int, stripping non-digit characters.

    Returns 0 if the string contains no digits.
    """
    digits = re.sub(r"[^0-9]", "", raw.strip())
    return int(digits) if digits else 0
