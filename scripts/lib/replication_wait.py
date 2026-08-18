# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Decision and reporting helpers for the replication wait loop.

``wait_for_replication`` in :mod:`replication` polls a container until
replication looks finished.  This module owns everything about that
loop that does not itself talk to Docker: the progress and timeout
reporting, the error-streak counter, and the steady-state verdict.

Splitting them out keeps the poll loop readable as a sequence of
numbered steps while the wording of each report — which operators
read straight out of the workflow log — stays in one reviewable
place.

The loop combines **three complementary completion signals**, one
per decision helper below:

1. **Repo count + log completions + content size** — the "classic"
   check, implemented by :func:`classic_complete`.  If the repository
   count on disk meets the expected count, the pull-replication log
   shows completions for ≥ 90 % of repos, and disk usage exceeds the
   per-repo content threshold, replication is declared finished
   immediately.

2. **Steady-state detection** — implemented by
   :func:`steady_state_complete`.  A
   :class:`~replication_state.ReplicationSnapshot` is taken every poll
   cycle.  When the snapshot (completed count, disk usage in KB, log
   line count, repo count) has not changed for the stability window
   **and** there is meaningful content, replication is declared
   finished.  This handles the case where all repos are small (total
   < old 100 MB floor) or when the log shows periodic no-op fetch
   cycles that keep the file growing even though nothing is actually
   changing.

3. **Error detection** — implemented by :func:`next_error_streak`.
   Errors must fire on *two consecutive* polls (to avoid transient
   false positives) before the loop fails fast.
"""

from __future__ import annotations

import logging

from errors import ReplicationError
from replication_report import ReplicationErrorReport
from replication_state import ReplicationSnapshot

logger = logging.getLogger(__name__)


def log_wait_header(
    initial_count: int,
    project: str,
    expected_count: int,
    timeout: int,
    stability_window: int,
) -> None:
    """Log the starting conditions of a wait loop."""
    logger.info("  Initial repository count: %d", initial_count)
    if project:
        logger.info("  Project filter: %s", project)
    if expected_count > 0:
        logger.info("  Expected from remote: %d", expected_count)
        logger.info("  Waiting up to %ds for all repositories…", timeout)
    else:
        logger.info("  No expected count available, waiting for replication activity…")
    logger.info(
        "  Stability window: %ds (declare done when state is unchanging)",
        stability_window,
    )
    logger.info("")


def next_error_streak(
    report: ReplicationErrorReport,
    streak: int,
    *,
    debug: bool = False,
) -> int:
    """Advance the consecutive user-project-error counter.

    Returns the streak length after folding in *report*, or 0 when
    the report is clean.  The caller fails fast once the streak
    reaches 2; requiring two consecutive hits avoids aborting on a
    transient error that the next poll no longer sees.

    Failure gating distinguishes three sources, in order of
    confidence:

    * ``has_user_project_errors`` (authoritative per-event log,
      user projects) — the only signal counted here, and hence the
      only one that can ultimately fail the workflow.
    * ``has_magic_repo_errors`` (authoritative per-event log,
      All-Users / All-Projects / ...) — surfaced as warnings
      but never fatal: the source server's ACL on these repos
      is commonly stricter than its ACL on user projects, and
      a non-admin replication credential can fail there while
      user-project replication completes fine.  See
      ``ReplicationErrorReport.has_magic_repo_errors`` for the
      rationale.
    * ``has_advisory_errors`` (container ``docker logs``) —
      also informational only.
    """
    if not report.has_user_project_errors:
        return 0
    streak += 1
    if streak < 2 and debug:
        logger.debug(
            "  Transient replication error (attempt %d/2), will recheck",
            streak,
        )
    return streak


def log_persistent_errors() -> None:
    """Announce that the error streak reached the fail-fast threshold."""
    logger.error("")
    logger.error("  ❌ Persistent replication errors detected!")
    logger.error("")


def persistent_error_failure(
    slug: str,
    expected_count: int,
    actual_count: int,
    elapsed: int,
) -> ReplicationError:
    """Build the failure raised after two consecutive error polls."""
    return ReplicationError(
        f"Replication errors detected for {slug}",
        expected_count=expected_count,
        actual_count=actual_count,
        elapsed=elapsed,
    )


def classic_complete(
    snap: ReplicationSnapshot,
    counts: tuple[int, int],
    checks: tuple[bool, bool],
    *,
    debug: bool = False,
) -> bool:
    """Evaluate the "classic" completion signal and report the verdict.

    Parameters
    ----------
    snap:
        The current snapshot, used for the fetched-size figure.
    counts:
        ``(current_count, expected_count)``.
    checks:
        ``(has_content, log_ok)`` — the per-repo content-size
        threshold and the pull-replication-log completions check.

    Returns *True*, after logging the success banner, only when both
    checks passed.
    """
    current_count, expected_count = counts
    has_content, log_ok = checks
    if debug:
        logger.debug(
            "  has_content=%s, log_ok=%s, count=%d, expected=%d",
            has_content,
            log_ok,
            current_count,
            expected_count,
        )
    if not (has_content and log_ok):
        return False
    logger.info("")
    logger.info(
        "  ✅ Replication complete: %d/%d repositories",
        current_count,
        expected_count,
    )
    logger.info("  ✅ Content verified: %dMB fetched", snap.disk_usage_kb // 1024)
    return True


def log_unbounded_completion(current_count: int, current_size_mb: int) -> None:
    """Report success when no expected repository count was available."""
    logger.info("")
    logger.info(
        "  ✅ Replication complete: %d repositories (%dMB)",
        current_count,
        current_size_mb,
    )


def steady_state_complete(
    snap: ReplicationSnapshot,
    expected_count: int,
    current_count: int,
    stability_window: int,
    *,
    debug: bool = False,
) -> bool:
    """Decide whether an unchanging state counts as "replication done".

    Called only once the tracker reports the state has not moved for
    *stability_window* seconds.  Returns *True* (and logs the success
    banner) when there is meaningful content to show for it.
    """
    current_size_mb = snap.disk_usage_kb // 1024
    # State hasn't changed for stability_window seconds.
    # Check whether we have anything meaningful at all.
    has_any_content = snap.disk_usage_kb > 0 and snap.completed_count > 0

    # For expected-count scenarios: accept if count matches
    # even if the classic threshold didn't pass (covers the
    # "small repos" case where total disk < per-repo threshold).
    count_ok = (
        expected_count <= 0
        or current_count >= expected_count
        or snap.completed_count >= expected_count
    )

    if has_any_content and count_ok:
        logger.info("")
        logger.info(
            "  ✅ Replication stable for %ds — declaring complete",
            stability_window,
        )
        logger.info(
            "     repos=%d, completed=%d, disk=%dMB, log_lines=%d",
            snap.repo_count,
            snap.completed_count,
            current_size_mb,
            snap.log_line_count,
        )
        return True

    if debug:
        logger.debug(
            "  Stable for %ds but not enough content (has_any_content=%s, count_ok=%s)",
            stability_window,
            has_any_content,
            count_ok,
        )
    return False


def log_progress(
    elapsed: int,
    timeout: int,
    snap: ReplicationSnapshot,
    expected_count: int,
    disk_human: str,
    stable_secs: int,
) -> None:
    """Log one periodic progress line for the wait loop."""
    completed = snap.completed_count
    if expected_count > 0:
        pct = (
            completed * 100 // expected_count
            if expected_count > 0 and completed > 0
            else 0
        )
        logger.info(
            "  [%ds/%ds] %d/%d unique repos completed (%d%%) disk=%s stable=%ds",
            elapsed,
            timeout,
            completed,
            expected_count,
            pct,
            disk_human,
            stable_secs,
        )
    else:
        logger.info(
            "  [%ds/%ds] %d unique repos completed disk=%s stable=%ds",
            elapsed,
            timeout,
            completed,
            disk_human,
            stable_secs,
        )


def log_pending_reasons(
    counts: tuple[int, int],
    has_content: bool,
    log_ok: bool,
    stability: tuple[bool, int, int],
) -> None:
    """Log which completion criteria are still unmet (helps debugging).

    Parameters
    ----------
    counts:
        ``(current_count, expected_count)``.
    has_content:
        Result of the per-repo content-size threshold check.
    log_ok:
        Result of the pull-replication-log completions check.
    stability:
        ``(is_stable, seconds_stable, stability_window)``.
    """
    current_count, expected_count = counts
    is_stable, stable_secs, stability_window = stability
    pending: list[str] = []
    if current_count < expected_count:
        pending.append(f"repo_count ({current_count}<{expected_count})")
    if not has_content:
        pending.append("content_threshold")
    if not log_ok:
        pending.append("log_completions")
    if not is_stable:
        pending.append(f"stability ({stable_secs}s<{stability_window}s)")
    if pending:
        logger.debug("    Waiting on: %s", ", ".join(pending))


def log_timeout_summary(
    timeout: int,
    final_count: int,
    expected_count: int,
    disk_human: str,
    final_stable: int,
    stability_window: int,
) -> None:
    """Report the final state after the wait loop ran out of time.

    Distinguishes "idle at timeout" from "still active at timeout"
    because the two call for opposite remedies: a stalled run means
    the content threshold is probably wrong for these repository
    sizes, whereas an active run just needs a longer timeout.
    """
    logger.error("")
    logger.error("  ❌ Timeout after %ds", timeout)
    logger.error("  Final: %d repositories", final_count)
    if expected_count > 0:
        logger.error("  Expected: %d", expected_count)

    logger.error("  Disk usage: %s", disk_human)
    logger.error(
        "  State was stable for %ds (window=%ds)",
        final_stable,
        stability_window,
    )
    if final_stable >= stability_window:
        logger.error(
            "  ⚠️ Replication appears idle — the data above may be "
            "the final state.  Check whether the content threshold "
            "is appropriate for your repository sizes."
        )
    else:
        logger.error(
            "  ℹ️ Replication was still active at timeout — "
            "consider increasing REPLICATION_WAIT_TIMEOUT."
        )
    logger.error("")
