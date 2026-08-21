# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Diagnostic output for the replication wait loop.

Split out of :mod:`replication`: everything the poll loop in
:mod:`replication_wait` writes to the log, plus the two failure paths
that dump debugging context before raising.  Keeping it here leaves
:func:`replication.wait_for_replication` as the decision logic alone.

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
from typing import Any, NoReturn

import replication
from docker_manager import DockerManager
from errors import ReplicationError
from replication_model import (
    ReplicationSnapshot,
    _SeenMatches,
    _StabilityTracker,
    _WaitSettings,
)
from replication_report import ErrorMatch, ReplicationErrorReport

logger = logging.getLogger(__name__)

_LOG_FILE_SOURCE = ("pull_replication_log",)


# ---------------------------------------------------------------------------
# Error digest
# ---------------------------------------------------------------------------


def _emit_new_matches(
    report: ReplicationErrorReport,
    candidates: list[ErrorMatch],
    seen: set[str],
    heading: str,
    level: int,
    **filters: Any,
) -> None:
    """Log the *candidates* not yet in *seen*, under *heading*.

    The heading is written only when there is something new to put
    under it, and ``only_lines`` scopes the body to those same new
    lines so a match discovered on an earlier poll is never printed
    twice.
    """
    new_lines = [m.line for m in candidates if m.line not in seen]
    if not new_lines:
        return
    logger.log(level, heading)
    for diag in report.format_matches(only_lines=set(new_lines), **filters):
        logger.log(level, diag)
    seen.update(new_lines)


def _log_error_digest(
    report: ReplicationErrorReport,
    seen: _SeenMatches,
    *,
    debug: bool,
) -> None:
    """Report what the latest error scan matched, under four headings.

    Always surface what matched so subsequent debug runs know which
    source / regex fired — the 50-line tail dumped on failure rarely
    contains the matching line itself.  Each heading is scoped to its
    own source / classification via ``format_matches`` filters, so the
    same line never appears under more than one heading.

    The headings, in ascending order of severity:

    * advisory (container ``docker logs``) — informational, and only
      shown when *debug* is set;
    * soft failures (e.g. ``InexistentRefTransportException``) — an
      expected consequence of the magic-repo remote's enumerated
      refspec list spanning two heterogeneous magic projects and
      tightly-ACL'd source servers;
    * magic-repo errors (``All-Users`` / ``All-Projects`` / …) — the
      source server's ACL on these is commonly stricter than on user
      projects, so a non-admin credential can fail here while
      user-project replication completes fine;
    * authoritative user-project errors — the only class the caller
      counts toward failing the workflow.
    """
    if report.has_advisory_errors and debug:
        _emit_new_matches(
            report,
            report.container_log_matches,
            seen.advisory,
            "  Advisory replication signals (informational):",
            logging.DEBUG,
            sources=("container_logs",),
        )
    if report.has_soft_failures:
        _emit_new_matches(
            report,
            [m for m in report.log_file_matches if m.is_soft_failure],
            seen.soft_failure,
            "  Soft replication failures (refs missing on remote "
            "or hidden by source ACL; will not fail verification):",
            logging.WARNING,
            sources=_LOG_FILE_SOURCE,
            soft_failure=True,
        )
    if report.has_magic_repo_errors:
        _emit_new_matches(
            report,
            [
                m
                for m in report.log_file_matches
                if m.is_magic_repo and not m.is_soft_failure
            ],
            seen.magic_repo,
            "  Magic-repo replication errors (degraded NoteDb "
            "rendering; user-project replication unaffected):",
            logging.WARNING,
            sources=_LOG_FILE_SOURCE,
            magic_repo=True,
            soft_failure=False,
        )
    if report.has_user_project_errors:
        _emit_new_matches(
            report,
            [
                m
                for m in report.log_file_matches
                if not m.is_magic_repo and not m.is_soft_failure
            ],
            seen.user_project,
            "  Authoritative replication-log errors:",
            logging.WARNING,
            sources=_LOG_FILE_SOURCE,
            magic_repo=False,
            soft_failure=False,
        )


# ---------------------------------------------------------------------------
# Progress output
# ---------------------------------------------------------------------------


def _log_wait_preamble(
    settings: _WaitSettings,
    initial_count: int,
    project: str,
) -> None:
    """Announce what the wait loop is waiting for, and for how long."""
    logger.info("  Initial repository count: %d", initial_count)
    if project:
        logger.info("  Project filter: %s", project)
    if settings.expected_count > 0:
        logger.info("  Expected from remote: %d", settings.expected_count)
        logger.info("  Waiting up to %ds for all repositories…", settings.timeout)
    else:
        logger.info("  No expected count available, waiting for replication activity…")
    logger.info(
        "  Stability window: %ds (declare done when state is unchanging)",
        settings.stability_window,
    )
    logger.info("")


def _log_progress(
    docker: DockerManager,
    cid: str,
    settings: _WaitSettings,
    snap: ReplicationSnapshot,
    tracker: _StabilityTracker,
    elapsed: int,
) -> None:
    """Emit one periodic progress line."""
    disk_human = replication.get_git_disk_usage_human(docker, cid)
    completed = snap.completed_count
    expected_count = settings.expected_count
    stable_secs = int(tracker.seconds_stable)

    if expected_count > 0:
        pct = (
            completed * 100 // expected_count
            if expected_count > 0 and completed > 0
            else 0
        )
        logger.info(
            "  [%ds/%ds] %d/%d unique repos completed (%d%%) disk=%s stable=%ds",
            elapsed,
            settings.timeout,
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
            settings.timeout,
            completed,
            disk_human,
            stable_secs,
        )


def _log_pending_reasons(
    docker: DockerManager,
    cid: str,
    settings: _WaitSettings,
    snap: ReplicationSnapshot,
    tracker: _StabilityTracker,
    now: float,
) -> None:
    """List the completion criteria that have not been met yet.

    Called only under ``debug`` — each reason costs two more container
    round trips, which is not worth paying on every fifteen-second tick
    of a normal run.
    """
    expected_count = settings.expected_count
    current_count = snap.repo_count
    has_content = replication.check_replication_has_content(docker, cid, expected_count)
    log_ok = replication.check_pull_replication_log(
        docker, cid, expected_count, debug=False
    )
    pending: list[str] = []
    if current_count < expected_count:
        pending.append(f"repo_count ({current_count}<{expected_count})")
    if not has_content:
        pending.append("content_threshold")
    if not log_ok:
        pending.append("log_completions")
    if not tracker.is_stable(now):
        stable_secs = int(tracker.seconds_stable)
        pending.append(f"stability ({stable_secs}s<{settings.stability_window}s)")
    if pending:
        logger.debug("    Waiting on: %s", ", ".join(pending))


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def _raise_persistent_error(
    docker: DockerManager,
    cid: str,
    settings: _WaitSettings,
    elapsed: int,
) -> NoReturn:
    """Dump debugging context, then abort on repeated user-project errors."""
    logger.error("")
    logger.error("  ❌ Persistent replication errors detected!")
    logger.error("")
    logger.error("  Debugging info:")
    repos = replication.list_repositories(docker, cid, max_items=10)
    for line in repos.splitlines():
        logger.error("    %s", line)
    logger.error("")
    log_tail = replication.show_pull_replication_log(docker, cid)
    for line in log_tail.splitlines():
        logger.error("    %s", line)

    raise ReplicationError(
        f"Replication errors detected for {settings.slug}",
        expected_count=settings.expected_count,
        actual_count=replication.count_repositories(docker, cid),
        elapsed=elapsed,
    )


def _raise_timeout(
    docker: DockerManager,
    cid: str,
    settings: _WaitSettings,
    tracker: _StabilityTracker,
) -> NoReturn:
    """Dump the final state, then abort because the wait budget ran out."""
    final_snap = replication.take_snapshot(docker, cid)
    final_count = final_snap.repo_count
    final_stable = int(tracker.seconds_stable)

    logger.error("")
    logger.error("  ❌ Timeout after %ds", settings.timeout)
    logger.error("  Final: %d repositories", final_count)
    if settings.expected_count > 0:
        logger.error("  Expected: %d", settings.expected_count)

    disk_human = replication.get_git_disk_usage_human(docker, cid)
    logger.error("  Disk usage: %s", disk_human)
    logger.error(
        "  State was stable for %ds (window=%ds)",
        final_stable,
        settings.stability_window,
    )
    if final_stable >= settings.stability_window:
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

    # Debugging info
    logger.error("  Debugging info:")
    repos = replication.list_repositories(docker, cid, max_items=10)
    for line in repos.splitlines():
        logger.error("    %s", line)
    logger.error("")
    log_tail = replication.show_pull_replication_log(docker, cid)
    for line in log_tail.splitlines():
        logger.error("    %s", line)

    raise ReplicationError(
        f"Replication timed out for {settings.slug} after {settings.timeout}s "
        f"(got {final_count}/{settings.expected_count} repositories)",
        expected_count=settings.expected_count,
        actual_count=final_count,
        elapsed=settings.timeout,
    )
