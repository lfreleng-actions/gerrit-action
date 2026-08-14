# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result types and final reporting for the local replication harness.

Owns the harness's reporting vocabulary — the outcome of a single
check (:class:`CheckResult`) and the aggregate for one scenario
(:class:`ScenarioResult`) — plus the end-of-run summary and the exit
code derived from it.

Results are deliberately plain values with no I/O of their own so the
scenario runner can accumulate partial results even when a scenario
aborts part-way through.
"""

from __future__ import annotations

import dataclasses
import logging

from harness_scenarios import Scenario

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class CheckResult:
    """Outcome of a single test within a scenario."""

    name: str
    passed: bool
    message: str = ""
    elapsed_s: float = 0.0

    def __str__(self) -> str:
        icon = "✅" if self.passed else "❌"
        msg = f" — {self.message}" if self.message else ""
        timing = f" ({self.elapsed_s:.1f}s)" if self.elapsed_s else ""
        return f"  {icon} {self.name}{msg}{timing}"


@dataclasses.dataclass
class ScenarioResult:
    """Aggregated result for one scenario."""

    scenario: Scenario
    tests: list[CheckResult] = dataclasses.field(default_factory=list)
    container_started: bool = False
    gerrit_ready: bool = False
    error: str = ""

    @property
    def passed(self) -> bool:
        if not self.container_started or not self.gerrit_ready:
            return False
        return all(t.passed for t in self.tests)

    @property
    def total_elapsed(self) -> float:
        return sum(t.elapsed_s for t in self.tests)


def print_summary(
    scenario_results: list[ScenarioResult],
    tunnel_results: list[CheckResult],
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

    for sr in scenario_results:
        _log_scenario_result(sr)

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

    if total_failed > 0:
        logger.info("")
        logger.info("❌ SOME TESTS FAILED")
        return 1

    logger.info("")
    logger.info("✅ ALL TESTS PASSED")
    return 0


def _log_scenario_result(sr: ScenarioResult) -> None:
    """Log the per-scenario block of the final summary."""
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
