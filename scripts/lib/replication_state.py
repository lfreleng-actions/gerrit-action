# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Value types describing replication progress and per-instance outcomes.

This module owns the plain data model shared by the replication
trigger and verification flows:

- :class:`TriggerResult` / :class:`VerificationResult` — the outcome
  records returned for a single Gerrit instance.
- :class:`ReplicationSnapshot` — a point-in-time reading of the
  observable replication counters inside a container.
- :class:`_StabilityTracker` — the steady-state detector that decides
  when those counters have stopped moving.

Nothing here talks to Docker or performs I/O; keeping the data model
free of side effects lets the wait loop be reasoned about (and
tested) independently of the container probes that feed it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Default number of seconds with no state change before declaring
# replication "stable" (i.e. nothing new is being fetched).
_STABILITY_WINDOW_SECONDS = 45


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
