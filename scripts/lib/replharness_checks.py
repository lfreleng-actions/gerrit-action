# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Per-scenario assertions run against a live Gerrit container.

Split out of ``scripts/test-replication-local.py``.  Each ``_test_*``
helper probes one aspect of the replication detection logic and returns
a :class:`~replharness_model.TestResult`;
:func:`run_scenario_checks` runs them in the order the harness expects.
Every name here is re-exported from the harness entry point.
"""

from __future__ import annotations

import logging
import time

from docker_manager import DockerManager
from errors import ReplicationError
from replharness_model import (
    Scenario,
    ScenarioRunOptions,
    TestResult,
)
from replication import (
    ReplicationSnapshot,
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

# Ceiling on how long the steady-state check waits for quiescence,
# expressed as a multiple of the stability window.  Replication that
# is genuinely still active needs room to settle, but an unbounded
# wait would only re-create the timeout the harness exists to catch.
_STEADY_STATE_BUDGET_FACTOR = 3


def _describe_change(before: ReplicationSnapshot, after: ReplicationSnapshot) -> str:
    """Summarise what moved between two snapshots, for the report."""
    if before.is_same_as(after):
        return "state unchanged"

    changed_fields: list[str] = []
    if before.completed_count != after.completed_count:
        changed_fields.append(
            f"completed {before.completed_count}->{after.completed_count}"
        )
    if before.disk_usage_kb != after.disk_usage_kb:
        changed_fields.append(f"disk {before.disk_usage_kb}->{after.disk_usage_kb}KB")
    if before.log_line_count != after.log_line_count:
        changed_fields.append(
            f"log_lines {before.log_line_count}->{after.log_line_count}"
        )
    if before.repo_count != after.repo_count:
        changed_fields.append(f"repos {before.repo_count}->{after.repo_count}")
    return "changed: " + ", ".join(changed_fields)


def _test_content_threshold(
    docker: DockerManager, cid: str, scenario: Scenario
) -> TestResult:
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
            return TestResult(
                name="content_threshold (small-repo regression)",
                passed=True,
                message=f"disk={disk} >= {threshold_mb}MB threshold — old 100MB floor would have FAILED",
                elapsed_s=elapsed,
            )
        else:
            return TestResult(
                name="content_threshold (small-repo regression)",
                passed=False,
                message=f"disk={disk}, threshold={threshold_mb}MB, disk_kb={disk_kb}",
                elapsed_s=elapsed,
            )
    else:
        return TestResult(
            name="content_threshold",
            passed=result,
            message=f"disk={get_git_disk_usage_human(docker, cid)}",
            elapsed_s=elapsed,
        )


def _test_steady_state_detection(
    docker: DockerManager,
    cid: str,
    _scenario: Scenario,
    stability_window: int,
) -> TestResult:
    """Verify that the stability tracker detects quiescence.

    Samples until the tracker reports stable, or until a budget of
    ``_STEADY_STATE_BUDGET_FACTOR × stability_window`` seconds is
    exhausted.  Reaching stability passes; exhausting the budget
    fails.

    The check previously took a single sample and returned
    ``passed=True`` on both branches, treating "replication is still
    active" as a pass because one sample cannot tell that apart from
    "steady-state detection is broken".  That made it unfalsifiable:
    it contributed a green tick to the summary regardless of outcome,
    while being named and counted as a regression test for exactly
    the behaviour ``_StabilityTracker`` exists to provide.  Sampling
    over a bounded budget resolves the ambiguity instead of
    swallowing it.
    """
    start = time.time()
    tracker = _StabilityTracker(window=stability_window)

    # Sample roughly three times per window, but no more often than
    # every five seconds for windows that can afford it.  The floor
    # scales down for very short windows: a flat five-second interval
    # made ``STABILITY_WINDOW=1`` unsatisfiable, because one interval
    # already exceeded the whole three-second budget and the
    # within-budget gate then rejected the first repeated observation.
    #
    # The trailing ``1`` keeps the interval positive.  The environment
    # value is parsed as an unrestricted integer, so a window of ``0``
    # (or a negative one) reaches here and would otherwise divide by
    # zero below, crashing the harness before it could print its
    # summary.  Such a window simply fails the check instead.
    interval = max(min(5, stability_window), stability_window // 3, 1)
    budget = stability_window * _STEADY_STATE_BUDGET_FACTOR
    # Bound the loop by sample count as well as wall clock so it
    # always terminates, even if the clock misbehaves.
    max_samples = max(budget // interval, 1)

    previous = take_snapshot(docker, cid)
    tracker.update(previous)
    latest = previous
    # Observation times are recorded *after* each snapshot returns.
    # ``ReplicationSnapshot.timestamp`` is set before its four Docker
    # queries run, so it says when collection started rather than when
    # the state was actually seen; crediting a stability window from it
    # can report far more quiet time than was observed.
    previous_observed_at = time.time()
    unchanged_since: float | None = None

    for sample in range(max_samples + 1):
        now = time.time()
        elapsed = now - start
        # Three conditions must hold before stability is credited.
        #
        # ``unchanged_since`` — the state has repeated, and this is the
        # post-collection moment it was first observed in its current
        # form.  Requiring a full window to have passed since then
        # means the quiet period was genuinely watched, rather than
        # inferred from a snapshot that was already stale on arrival.
        #
        # ``elapsed < budget`` — a pass reported after the advertised
        # budget has run out would contradict the budget the failure
        # message quotes.
        #
        # ``tracker.is_stable`` — the verdict actually under test.  The
        # observation bookkeeping above is deliberately independent of
        # it, so a regression in the tracker cannot be masked by this
        # check agreeing with it.
        if (
            unchanged_since is not None
            and now - unchanged_since >= stability_window
            and elapsed < budget
            and tracker.is_stable(now)
        ):
            return TestResult(
                name="steady_state_detection",
                passed=True,
                message=(
                    f"stable=True after {elapsed:.0f}s "
                    f"({_describe_change(previous, latest)})"
                ),
                elapsed_s=elapsed,
            )
        if sample == max_samples or elapsed >= budget:
            # Stop scheduling further intervals once the advertised
            # wall-clock budget is spent.  ``max_samples`` remains as
            # the guard against a misbehaving clock, but on its own it
            # would let a slow ``take_snapshot`` — four Docker calls
            # per sample — overrun the budget several times over.
            break
        time.sleep(interval)
        previous = latest
        latest = take_snapshot(docker, cid)
        observed_at = time.time()
        tracker.update(latest)
        if latest.is_same_as(previous):
            # Date the quiet period from when this state was *first*
            # observed, not from now.
            if unchanged_since is None:
                unchanged_since = previous_observed_at
        else:
            unchanged_since = None
        previous_observed_at = observed_at

    elapsed = time.time() - start
    return TestResult(
        name="steady_state_detection",
        passed=False,
        message=(
            f"stable=False after {elapsed:.0f}s — replication still "
            f"active past the {budget}s budget "
            f"(window={stability_window}s, "
            f"{_describe_change(previous, latest)})"
        ),
        elapsed_s=elapsed,
    )


def _test_wait_for_replication(
    docker: DockerManager,
    cid: str,
    scenario: Scenario,
    timeout: int,
    stability_window: int,
) -> TestResult:
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
                return TestResult(
                    name="wait_for_replication",
                    passed=True,
                    message=f"completed in {elapsed:.0f}s (timeout={timeout}s) — early exit ✅",
                    elapsed_s=elapsed,
                )
            else:
                return TestResult(
                    name="wait_for_replication",
                    passed=True,
                    message=f"completed in {elapsed:.0f}s (close to timeout={timeout}s) ⚠️",
                    elapsed_s=elapsed,
                )
        else:
            return TestResult(
                name="wait_for_replication",
                passed=False,
                message=f"returned False after {elapsed:.0f}s",
                elapsed_s=elapsed,
            )

    except ReplicationError as exc:
        elapsed = time.time() - start
        return TestResult(
            name="wait_for_replication",
            passed=False,
            message=f"raised ReplicationError after {elapsed:.0f}s: {exc}",
            elapsed_s=elapsed,
        )


def _test_snapshot_fields(docker: DockerManager, cid: str) -> TestResult:
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
        return TestResult(
            name="snapshot_fields",
            passed=False,
            message=f"bad fields: {', '.join(issues)}",
        )
    return TestResult(
        name="snapshot_fields",
        passed=True,
        message=(
            f"repos={snap.repo_count} completed={snap.completed_count} "
            f"disk={snap.disk_usage_kb}KB log_lines={snap.log_line_count}"
        ),
    )


def _test_no_false_errors(docker: DockerManager, cid: str) -> TestResult:
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
        return TestResult(
            name="no_false_errors",
            passed=False,
            message=(
                f"check_replication_errors flagged matches: {diagnostics} "
                f"— log tail: {log_tail[:200]}"
            ),
        )
    return TestResult(
        name="no_false_errors",
        passed=True,
        message="no false positives from error detection",
    )


def _test_log_line_count(docker: DockerManager, cid: str) -> TestResult:
    """Verify ``get_log_line_count`` returns a positive value."""
    count = get_log_line_count(docker, cid)
    if count > 0:
        return TestResult(
            name="log_line_count",
            passed=True,
            message=f"{count} lines in pull_replication_log",
        )
    return TestResult(
        name="log_line_count",
        passed=False,
        message="0 lines — log may not have been created yet",
    )


def run_scenario_checks(
    docker: DockerManager,
    cid: str,
    scenario: Scenario,
    options: ScenarioRunOptions,
    results: list[TestResult],
) -> None:
    """Run every container-backed check, appending to *results*.

    Results are appended one at a time rather than returned in a batch
    so that a check raising part-way through still leaves the earlier
    outcomes on the scenario record.
    """
    logger.info("")
    logger.info("  Running tests…")
    logger.info("")

    # 1. Snapshot fields
    results.append(_test_snapshot_fields(docker, cid))

    # 2. Log line count
    results.append(_test_log_line_count(docker, cid))

    # 3. No false error detection
    results.append(_test_no_false_errors(docker, cid))

    # 4. Content threshold (regression test for 86MB/36-repo bug)
    results.append(_test_content_threshold(docker, cid, scenario))

    # 5. Steady-state detection
    results.append(
        _test_steady_state_detection(docker, cid, scenario, options.stability_window)
    )

    # 6. Full wait_for_replication — this is the integration test
    results.append(
        _test_wait_for_replication(
            docker, cid, scenario, options.timeout, options.stability_window
        )
    )
