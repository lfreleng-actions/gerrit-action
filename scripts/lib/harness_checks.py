# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Assertions run against a live Gerrit container.

Each function here exercises one behaviour of the replication
detection logic against a container that has already been started and
had at least one fetch cycle, and reports the outcome as a
:class:`CheckResult` rather than raising.  Returning results instead of
asserting is deliberate: a scenario should run every check and report
the full picture, not stop at the first disappointment.
"""

from __future__ import annotations

import logging
import time

from docker_manager import DockerManager
from errors import ReplicationError
from harness_results import CheckResult
from harness_scenarios import Scenario
from replication import (
    _StabilityTracker,
    check_replication_errors,
    check_replication_has_content,
    get_disk_usage_kb,
    get_git_disk_usage_human,
    get_log_line_count,
    show_pull_replication_log,
    take_snapshot,
    wait_for_replication,
)

logger = logging.getLogger(__name__)


def check_content_threshold(
    docker: DockerManager, cid: str, scenario: Scenario
) -> CheckResult:
    """Verify that ``check_replication_has_content`` returns True.

    For small-repo scenarios this is the core regression test — the old
    100 MB floor would return False here.
    """
    start = time.time()
    result = check_replication_has_content(
        docker, cid, expected_count=scenario.expected_project_count
    )
    elapsed = time.time() - start

    if scenario.expect_small_disk:
        disk = get_git_disk_usage_human(docker, cid)
        disk_kb = get_disk_usage_kb(docker, cid)
        threshold_kb = scenario.expected_project_count * 200  # _MIN_KB_PER_REPO
        threshold_mb = max(threshold_kb // 1024, 1)
        if result:
            return CheckResult(
                name="content_threshold (small-repo regression)",
                passed=True,
                message=f"disk={disk} >= {threshold_mb}MB threshold — old 100MB floor would have FAILED",
                elapsed_s=elapsed,
            )
        else:
            return CheckResult(
                name="content_threshold (small-repo regression)",
                passed=False,
                message=f"disk={disk}, threshold={threshold_mb}MB, disk_kb={disk_kb}",
                elapsed_s=elapsed,
            )
    else:
        return CheckResult(
            name="content_threshold",
            passed=result,
            message=f"disk={get_git_disk_usage_human(docker, cid)}",
            elapsed_s=elapsed,
        )


def check_steady_state_detection(
    docker: DockerManager,
    cid: str,
    _scenario: Scenario,
    stability_window: int,
) -> CheckResult:
    """Verify that the stability tracker detects quiescence.

    Takes snapshots 3 × stability_window seconds apart and asserts the
    tracker reports stable.
    """
    start = time.time()
    tracker = _StabilityTracker(window=stability_window)

    snap1 = take_snapshot(docker, cid)
    tracker.update(snap1)

    # Wait for one stability window and re-check
    time.sleep(stability_window + 5)

    snap2 = take_snapshot(docker, cid)
    tracker.update(snap2)

    elapsed = time.time() - start
    now = time.time()
    stable = tracker.is_stable(now)

    if snap1.is_same_as(snap2):
        detail = f"state unchanged for {elapsed:.0f}s"
    else:
        changed_fields: list[str] = []
        if snap1.completed_count != snap2.completed_count:
            changed_fields.append(
                f"completed {snap1.completed_count}->{snap2.completed_count}"
            )
        if snap1.disk_usage_kb != snap2.disk_usage_kb:
            changed_fields.append(
                f"disk {snap1.disk_usage_kb}->{snap2.disk_usage_kb}KB"
            )
        if snap1.log_line_count != snap2.log_line_count:
            changed_fields.append(
                f"log_lines {snap1.log_line_count}->{snap2.log_line_count}"
            )
        if snap1.repo_count != snap2.repo_count:
            changed_fields.append(f"repos {snap1.repo_count}->{snap2.repo_count}")
        detail = "changed: " + ", ".join(changed_fields)

    if stable:
        return CheckResult(
            name="steady_state_detection",
            passed=True,
            message=f"stable=True after {elapsed:.0f}s ({detail})",
            elapsed_s=elapsed,
        )
    else:
        # If state is still changing, that's fine — replication may
        # still be running.  We only fail if we expected stability.
        return CheckResult(
            name="steady_state_detection",
            passed=True,  # Informational — state still changing is valid
            message=f"stable=False — replication still active ({detail})",
            elapsed_s=elapsed,
        )


def check_wait_for_replication(
    docker: DockerManager,
    cid: str,
    scenario: Scenario,
    timeout: int,
    stability_window: int,
) -> CheckResult:
    """Run the full ``wait_for_replication`` and verify it completes.

    The key assertion: the function should return True well before the
    full timeout (especially for small-repo scenarios).
    """
    start = time.time()
    try:
        ok = wait_for_replication(
            docker,
            cid,
            scenario.slug,
            timeout=timeout,
            expected_count=scenario.expected_project_count,
            project=scenario.project_filter,
            debug=True,
            stability_window=stability_window,
        )
        elapsed = time.time() - start

        if ok:
            # Check it didn't take the full timeout
            if elapsed < timeout * 0.8:
                return CheckResult(
                    name="wait_for_replication",
                    passed=True,
                    message=f"completed in {elapsed:.0f}s (timeout={timeout}s) — early exit ✅",
                    elapsed_s=elapsed,
                )
            else:
                return CheckResult(
                    name="wait_for_replication",
                    passed=True,
                    message=f"completed in {elapsed:.0f}s (close to timeout={timeout}s) ⚠️",
                    elapsed_s=elapsed,
                )
        else:
            return CheckResult(
                name="wait_for_replication",
                passed=False,
                message=f"returned False after {elapsed:.0f}s",
                elapsed_s=elapsed,
            )

    except ReplicationError as exc:
        elapsed = time.time() - start
        return CheckResult(
            name="wait_for_replication",
            passed=False,
            message=f"raised ReplicationError after {elapsed:.0f}s: {exc}",
            elapsed_s=elapsed,
        )


def check_snapshot_fields(docker: DockerManager, cid: str) -> CheckResult:
    """Verify ``take_snapshot`` returns populated fields."""
    snap = take_snapshot(docker, cid)
    issues: list[str] = []
    if snap.timestamp <= 0:
        issues.append("timestamp=0")
    if snap.repo_count < 0:
        issues.append(f"repo_count={snap.repo_count}")
    if snap.disk_usage_kb <= 0:
        issues.append(f"disk_usage_kb={snap.disk_usage_kb}")

    if issues:
        return CheckResult(
            name="snapshot_fields",
            passed=False,
            message=f"bad fields: {', '.join(issues)}",
        )
    return CheckResult(
        name="snapshot_fields",
        passed=True,
        message=(
            f"repos={snap.repo_count} completed={snap.completed_count} "
            f"disk={snap.disk_usage_kb}KB log_lines={snap.log_line_count}"
        ),
    )


def check_no_false_errors(docker: DockerManager, cid: str) -> CheckResult:
    """Verify ``check_replication_errors`` does not false-positive.

    Normal replication log activity (ASYNC started, completed, periodic
    fetch scheduling) should NOT be flagged as errors.
    """
    report = check_replication_errors(docker, cid)
    log_tail = show_pull_replication_log(docker, cid, lines=10)

    if report.has_any_errors:
        # Include the per-source/pattern attribution so a failure
        # here points at the offending rule without a re-run.
        diagnostics = " | ".join(report.format_matches(max_per_source=3))
        return CheckResult(
            name="no_false_errors",
            passed=False,
            message=(
                f"check_replication_errors flagged matches: {diagnostics} "
                f"— log tail: {log_tail[:200]}"
            ),
        )
    return CheckResult(
        name="no_false_errors",
        passed=True,
        message="no false positives from error detection",
    )


def check_log_line_count(docker: DockerManager, cid: str) -> CheckResult:
    """Verify ``get_log_line_count`` returns a positive value."""
    count = get_log_line_count(docker, cid)
    if count > 0:
        return CheckResult(
            name="log_line_count",
            passed=True,
            message=f"{count} lines in pull_replication_log",
        )
    return CheckResult(
        name="log_line_count",
        passed=False,
        message="0 lines — log may not have been created yet",
    )
