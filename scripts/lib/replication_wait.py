# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The poll loop that decides when replication has finished.

Split out of :mod:`replication`, which re-exports
:func:`wait_for_replication`.  This module holds the loop and the
predicates it tests each cycle; everything it prints, and the two
failure paths, live in :mod:`replication_diagnostics`.

The steps here reach their collaborators through the :mod:`replication`
facade rather than importing them directly.  Every probe in this
package is re-exported there, and callers (the test suite in
particular) rebind those attributes to stub out a step; resolving the
name on the facade at call time is what keeps that substitution
working now the probes live in sibling modules.  :mod:`replication`
imports this module for its own re-exports, so the import below is
deliberately circular; only attribute lookups happen at call time,
never at import time.
"""

from __future__ import annotations

import logging
import time

import replication
from docker_manager import DockerManager
from replication_diagnostics import (
    _log_error_digest,
    _log_pending_reasons,
    _log_progress,
    _log_wait_preamble,
    _raise_persistent_error,
    _raise_timeout,
)
from replication_model import (
    ReplicationSnapshot,
    _SeenMatches,
    _StabilityTracker,
    _WaitSettings,
)
from replication_patterns import _STABILITY_WINDOW_SECONDS

logger = logging.getLogger(__name__)


def _check_for_errors(
    docker: DockerManager,
    cid: str,
    settings: _WaitSettings,
    seen: _SeenMatches,
    consecutive_errors: int,
    elapsed: int,
) -> int:
    """Scan for errors and return the updated consecutive-error streak.

    Failure gating distinguishes three sources, in order of
    confidence: authoritative user-project errors from the per-event
    log gate the streak returned here, while magic-repo errors from
    the same log and advisory matches from the container log are only
    reported.  Two consecutive polls with user-project errors abort
    the wait; a single one is treated as transient.
    """
    report = replication.check_replication_errors(docker, cid)
    _log_error_digest(report, seen, debug=settings.debug)

    if not report.has_user_project_errors:
        return 0

    streak = consecutive_errors + 1
    if streak >= 2:
        _raise_persistent_error(docker, cid, settings, elapsed)
    if settings.debug:
        logger.debug(
            "  Transient replication error (attempt %d/2), will recheck",
            streak,
        )
    return streak


def _completion_reached(
    docker: DockerManager,
    cid: str,
    settings: _WaitSettings,
    snap: ReplicationSnapshot,
) -> bool:
    """Test the classic signal: repo count, log completions and content size."""
    expected_count = settings.expected_count
    current_count = snap.repo_count
    current_size_mb = snap.disk_usage_kb // 1024

    if expected_count > 0 and current_count >= expected_count:
        has_content = replication.check_replication_has_content(
            docker, cid, expected_count
        )
        log_ok = replication.check_pull_replication_log(
            docker, cid, expected_count, debug=settings.debug
        )

        if settings.debug:
            logger.debug(
                "  has_content=%s, log_ok=%s, count=%d, expected=%d",
                has_content,
                log_ok,
                current_count,
                expected_count,
            )

        if has_content and log_ok:
            logger.info("")
            logger.info(
                "  ✅ Replication complete: %d/%d repositories",
                current_count,
                expected_count,
            )
            logger.info("  ✅ Content verified: %dMB fetched", current_size_mb)
            return True

    # No expected count — check for content growth and log activity
    if (
        expected_count <= 0
        and replication.check_replication_has_content(docker, cid, 0)
        and replication.check_pull_replication_log(docker, cid, debug=settings.debug)
    ):
        logger.info("")
        logger.info(
            "  ✅ Replication complete: %d repositories (%dMB)",
            current_count,
            current_size_mb,
        )
        return True

    return False


def _steady_state_reached(
    settings: _WaitSettings,
    snap: ReplicationSnapshot,
    tracker: _StabilityTracker,
    now: float,
) -> bool:
    """Test whether an unchanging state with real content means "done"."""
    if not tracker.is_stable(now):
        return False

    # State hasn't changed for stability_window seconds.
    # Check whether we have anything meaningful at all.
    has_any_content = snap.disk_usage_kb > 0 and snap.completed_count > 0

    # For expected-count scenarios: accept if count matches
    # even if the classic threshold didn't pass (covers the
    # "small repos" case where total disk < per-repo threshold).
    count_ok = (
        settings.expected_count <= 0
        or snap.repo_count >= settings.expected_count
        or snap.completed_count >= settings.expected_count
    )

    if has_any_content and count_ok:
        logger.info("")
        logger.info(
            "  ✅ Replication stable for %ds — declaring complete",
            settings.stability_window,
        )
        logger.info(
            "     repos=%d, completed=%d, disk=%dMB, log_lines=%d",
            snap.repo_count,
            snap.completed_count,
            snap.disk_usage_kb // 1024,
            snap.log_line_count,
        )
        return True

    if settings.debug:
        logger.debug(
            "  Stable for %ds but not enough content (has_any_content=%s, count_ok=%s)",
            settings.stability_window,
            has_any_content,
            count_ok,
        )
    return False


def wait_for_replication(
    docker: DockerManager,
    cid: str,
    slug: str,
    timeout: int,
    expected_count: int = 0,
    project: str = "",
    debug: bool = False,
    stability_window: int = _STABILITY_WINDOW_SECONDS,
) -> bool:
    """Wait for replication to complete for a single instance.

    The function uses **three complementary signals** to decide when
    replication is finished:

    1. **Repo count + log completions + content size** — the "classic"
       check.  If the repository count on disk meets the expected count,
       the pull-replication log shows completions for ≥ 90 % of repos,
       and disk usage exceeds the per-repo content threshold, we
       declare success immediately.

    2. **Steady-state detection** — a :class:`ReplicationSnapshot` is
       taken every poll cycle.  When the snapshot (completed count,
       disk usage in KB, log line count, repo count) has not changed
       for *stability_window* seconds **and** there is meaningful
       content, we declare success.  This handles the case where all
       repos are small (total < old 100 MB floor) or when the log
       shows periodic no-op fetch cycles that keep the file growing
       even though nothing is actually changing.

    3. **Error detection** — if ``check_replication_errors`` fires
       on *two consecutive* polls (to avoid transient false positives)
       we fail fast.

    Returns *True* on success.
    Raises :class:`ReplicationError` on timeout or persistent errors.
    """
    settings = _WaitSettings(
        slug=slug,
        timeout=timeout,
        expected_count=expected_count,
        debug=debug,
        stability_window=stability_window,
    )
    elapsed = 0
    interval = 5
    consecutive_errors = 0  # require 2 in a row before failing fast
    initial_count = replication.count_repositories(docker, cid)
    seen = _SeenMatches()

    _log_wait_preamble(settings, initial_count, project)

    tracker = _StabilityTracker(window=stability_window)

    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval

        consecutive_errors = _check_for_errors(
            docker, cid, settings, seen, consecutive_errors, elapsed
        )

        snap = replication.take_snapshot(docker, cid)
        tracker.update(snap)

        if _completion_reached(docker, cid, settings, snap):
            return True

        now = time.time()
        if _steady_state_reached(settings, snap, tracker, now):
            return True

        if elapsed % 15 == 0:
            _log_progress(docker, cid, settings, snap, tracker, elapsed)
            # Log the reason we're still waiting (helps debugging)
            if debug and expected_count > 0:
                _log_pending_reasons(docker, cid, settings, snap, tracker, now)

    _raise_timeout(docker, cid, settings, tracker)
