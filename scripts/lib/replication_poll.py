# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The poll loop that waits for replication to finish on one instance.

This module owns the wait phase end to end: the timing of the poll
cycle, the order in which the three completion signals are consulted,
the fail-fast error streak, and the timeout failure.  The wording of
each report and the verdict of each individual signal live in
:mod:`replication_wait`; this module decides *when* to ask.

The loop never imports the container probes directly.  It is handed a
:class:`PollProbes` bundle by :mod:`replication`, which binds the
readers at call time.  Keeping that binding in one place means the
whole flow observes a single, stable set of readers, and it lets the
loop be exercised with plain stand-ins instead of a live container.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import replication_diagnostics as diagnostics
import replication_wait as wait_steps
from docker_manager import DockerManager
from errors import ReplicationError
from replication_report import ReplicationErrorReport
from replication_state import (
    _STABILITY_WINDOW_SECONDS,
    ReplicationSnapshot,
    _StabilityTracker,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollProbes:
    """The container readers the poll loop needs, supplied by the caller.

    ``check_pull_replication_log`` and ``check_replication_has_content``
    are typed loosely because the loop calls them with a varying number
    of optional arguments (``expected_count``, ``debug``); the rest take
    the container handle and id only.
    """

    take_snapshot: Callable[[DockerManager, str], ReplicationSnapshot]
    count_repositories: Callable[[DockerManager, str], int]
    check_replication_errors: Callable[[DockerManager, str], ReplicationErrorReport]
    check_pull_replication_log: Callable[..., bool]
    check_replication_has_content: Callable[..., bool]
    get_git_disk_usage_human: Callable[[DockerManager, str], str]
    list_repositories: Callable[..., str]
    show_pull_replication_log: Callable[..., str]


def _log_debug_dump(probes: PollProbes, docker: DockerManager, cid: str) -> None:
    """Dump a repository listing and the replication log, at error level."""
    logger.error("  Debugging info:")
    diagnostics.log_indented_error_lines(
        probes.list_repositories(docker, cid, max_items=10)
    )
    logger.error("")
    diagnostics.log_indented_error_lines(probes.show_pull_replication_log(docker, cid))


def _completion_reached(
    probes: PollProbes,
    docker: DockerManager,
    cid: str,
    snap: ReplicationSnapshot,
    expected_count: int,
    *,
    debug: bool = False,
) -> bool:
    """Evaluate the count / content / log completion signals for one poll.

    Covers signal 1 of the three described in :mod:`replication_wait`,
    in both its bounded (an expected repository count is known) and
    unbounded (no expected count available) forms.
    """
    current_count = snap.repo_count
    if expected_count > 0 and current_count >= expected_count:
        has_content = probes.check_replication_has_content(docker, cid, expected_count)
        log_ok = probes.check_pull_replication_log(
            docker, cid, expected_count, debug=debug
        )
        return wait_steps.classic_complete(
            snap,
            (current_count, expected_count),
            (has_content, log_ok),
            debug=debug,
        )

    # No expected count — check for content growth and log activity
    if (
        expected_count <= 0
        and probes.check_replication_has_content(docker, cid, 0)
        and probes.check_pull_replication_log(docker, cid, debug=debug)
    ):
        wait_steps.log_unbounded_completion(current_count, snap.disk_usage_kb // 1024)
        return True
    return False


def wait_for_replication(
    probes: PollProbes,
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

    Three complementary signals decide when replication is finished —
    the classic count/content/log check, steady-state detection, and
    error detection.  See the module docstring of
    :mod:`replication_wait` for what each signal covers and why it
    exists; the numbered steps below map onto it one-for-one.

    Returns *True* on success.
    Raises :class:`ReplicationError` on timeout or persistent errors.
    """
    elapsed = 0
    interval = 5
    consecutive_errors = 0  # require 2 in a row before failing fast
    seen = diagnostics.SeenMatchLines()
    tracker = _StabilityTracker(window=stability_window)
    wait_steps.log_wait_header(
        probes.count_repositories(docker, cid),
        project,
        expected_count,
        timeout,
        stability_window,
    )

    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval

        # ---- 1. Error check (require 2 consecutive hits) ----
        error_report = probes.check_replication_errors(docker, cid)
        diagnostics.log_new_error_matches(error_report, seen, debug=debug)
        consecutive_errors = wait_steps.next_error_streak(
            error_report, consecutive_errors, debug=debug
        )
        if consecutive_errors >= 2:
            wait_steps.log_persistent_errors()
            _log_debug_dump(probes, docker, cid)
            raise wait_steps.persistent_error_failure(
                slug,
                expected_count,
                probes.count_repositories(docker, cid),
                elapsed,
            )

        # ---- 2. Take a snapshot for steady-state tracking ----
        snap = probes.take_snapshot(docker, cid)
        tracker.update(snap)

        # ---- 3. Classic / unbounded completion checks ----
        if _completion_reached(probes, docker, cid, snap, expected_count, debug=debug):
            return True

        # ---- 4. Steady-state detection ----
        now = time.time()
        if tracker.is_stable(now) and wait_steps.steady_state_complete(
            snap, expected_count, snap.repo_count, stability_window, debug=debug
        ):
            return True

        # ---- 5. Progress reporting every 15 seconds ----
        if elapsed % 15 == 0:
            stability = (
                tracker.is_stable(now),
                int(tracker.seconds_stable),
                stability_window,
            )
            wait_steps.log_progress(
                elapsed,
                timeout,
                snap,
                expected_count,
                probes.get_git_disk_usage_human(docker, cid),
                stability[1],
            )
            # Log the reason we're still waiting (helps debugging)
            if debug and expected_count > 0:
                wait_steps.log_pending_reasons(
                    (snap.repo_count, expected_count),
                    probes.check_replication_has_content(docker, cid, expected_count),
                    probes.check_pull_replication_log(
                        docker, cid, expected_count, debug=False
                    ),
                    stability,
                )

    # ---- Timeout ----
    final_snap = probes.take_snapshot(docker, cid)
    wait_steps.log_timeout_summary(
        timeout,
        final_snap.repo_count,
        expected_count,
        probes.get_git_disk_usage_human(docker, cid),
        int(tracker.seconds_stable),
        stability_window,
    )
    _log_debug_dump(probes, docker, cid)

    raise ReplicationError(
        f"Replication timed out for {slug} after {timeout}s "
        f"(got {final_snap.repo_count}/{expected_count} repositories)",
        expected_count=expected_count,
        actual_count=final_snap.repo_count,
        elapsed=timeout,
    )
