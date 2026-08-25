# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Progress and summary rendering for the replication test harness.

Split out of ``scripts/test-replication-local.py``.  Announces each
scenario as it starts, then turns the collected scenario and tunnel
results into the human-readable report the harness prints last,
deriving the process exit code from them.  Every name here is
re-exported from the harness entry point.
"""

from __future__ import annotations

import logging

from replharness_model import (
    Scenario,
    ScenarioResult,
    ScenarioRunOptions,
    TestResult,
)

logger = logging.getLogger(__name__)


def log_scenario_banner(scenario: Scenario, options: ScenarioRunOptions) -> None:
    """Announce the scenario about to run and the timings it will use."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("SCENARIO: %s", scenario.name)
    logger.info("=" * 60)
    logger.info("  %s", scenario.description)
    logger.info("  Host:     %s", scenario.gerrit_host)
    logger.info("  API path: %s", scenario.api_path)
    if scenario.project_filter:
        logger.info("  Project:  %s", scenario.project_filter)
    logger.info("  Expected: %d repos", scenario.expected_project_count)
    logger.info("  Timeout:  %ds", options.timeout)
    logger.info("  Stability window: %ds", options.stability_window)
    logger.info("")


def print_summary(
    scenario_results: list[ScenarioResult],
    tunnel_results: list[TestResult],
) -> int:
    """Print a final summary and return an exit code."""
    total_tests = sum(len(sr.tests) for sr in scenario_results) + len(tunnel_results)
    total_passed = sum(
        sum(1 for t in sr.tests if t.passed) for sr in scenario_results
    ) + sum(1 for t in tunnel_results if t.passed)
    total_failed = total_tests - total_passed
    scenarios_passed = sum(1 for sr in scenario_results if sr.passed)
    scenarios_total = len(scenario_results)

    logger.info("")
    logger.info("=" * 60)
    logger.info("FINAL SUMMARY")
    logger.info("=" * 60)
    logger.info("")

    # Tunnel tests
    if tunnel_results:
        logger.info("Tunnel verification tests:")
        for t in tunnel_results:
            logger.info(str(t))
        logger.info("")

    # Scenario results
    for sr in scenario_results:
        icon = "✅" if sr.passed else "❌"
        logger.info(
            "%s Scenario: %s  (%d tests, %.0fs total)",
            icon,
            sr.scenario.name,
            len(sr.tests),
            sr.total_elapsed,
        )
        if sr.error:
            logger.info("  ERROR: %s", sr.error)
        if not sr.container_started:
            logger.info("  (container did not start)")
        elif not sr.gerrit_ready:
            logger.info("  (Gerrit did not become ready)")
        else:
            for t in sr.tests:
                logger.info(str(t))
        logger.info("")

    # Overall
    logger.info("-" * 60)
    logger.info(
        "Scenarios: %d/%d passed",
        scenarios_passed,
        scenarios_total,
    )
    logger.info(
        "Tests:     %d/%d passed  (%d failed)",
        total_passed,
        total_tests,
        total_failed,
    )
    logger.info("-" * 60)

    # A scenario that dies during start-up or readiness contributes no
    # TestResult entries at all, so it adds nothing to either count
    # above.  Reporting those separately keeps "never got far enough to
    # test anything" from reading like "every test passed".
    incomplete = [sr for sr in scenario_results if not sr.passed and not sr.tests]
    if incomplete:
        logger.info("")
        logger.info("Scenarios that failed before testing:")
        for sr in incomplete:
            logger.info("  ❌ %s — %s", sr.scenario.name, sr.error or "no tests ran")

    if total_failed > 0:
        logger.info("")
        logger.info("❌ SOME TESTS FAILED")
        return 1

    # Scenario-level outcomes gate the exit code too.  Deriving it from
    # the test tally alone let a scenario whose container never started
    # exit 0, which is exactly the case where a green result misleads
    # most.
    if scenarios_passed != scenarios_total:
        logger.info("")
        logger.info("❌ SOME SCENARIOS FAILED")
        return 1

    logger.info("")
    logger.info("✅ ALL TESTS PASSED")
    return 0
