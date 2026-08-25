# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the local replication harness reporting.

``scripts/test-replication-local.py`` is a developer harness rather
than an action entry point, but its summary decides the exit code a
developer reads a replication change through, so the aggregation is
worth pinning down.
"""

from __future__ import annotations

import pytest

# ``TestResult`` is aliased because pytest would otherwise try to
# collect the harness's result record as a test class.
from replharness_model import Scenario, ScenarioResult
from replharness_model import TestResult as HarnessTestResult
from replharness_report import print_summary


def _scenario(name: str = "lf-small") -> Scenario:
    """Build a minimal scenario record."""
    return Scenario(
        name=name,
        description="test scenario",
        gerrit_host="gerrit.example.org",
        api_path="/r",
    )


def _healthy_result(name: str = "lf-small") -> ScenarioResult:
    """Build a scenario result whose container came up and passed."""
    return ScenarioResult(
        scenario=_scenario(name),
        tests=[HarnessTestResult(name="snapshot_fields", passed=True)],
        container_started=True,
        gerrit_ready=True,
    )


class TestPrintSummary:
    """Exit-code aggregation across scenario and test outcomes."""

    def test_all_passing_exits_zero(self) -> None:
        """A fully successful run still reports success."""
        assert print_summary([_healthy_result()], []) == 0

    def test_failed_test_exits_non_zero(self) -> None:
        """A failing test fails the run, as before."""
        result = _healthy_result()
        result.tests.append(HarnessTestResult(name="log_line_count", passed=False))

        assert print_summary([result], []) == 1

    def test_scenario_failing_before_testing_exits_non_zero(self) -> None:
        """A container that never started must fail the run.

        Such a scenario produces no TestResult entries, so it adds
        nothing to the test tally the exit code used to be derived
        from — the harness printed ALL TESTS PASSED and exited 0.
        """
        dead = ScenarioResult(
            scenario=_scenario("onap"),
            container_started=False,
            error="No credentials for gerrit.onap.org",
        )

        assert print_summary([dead], []) == 1

    def test_gerrit_never_ready_exits_non_zero(self) -> None:
        """Readiness failures count the same way."""
        not_ready = ScenarioResult(
            scenario=_scenario("opnfv"),
            container_started=True,
            gerrit_ready=False,
            error="Gerrit did not become ready within 120s",
        )

        assert print_summary([not_ready], []) == 1

    def test_healthy_scenario_alongside_dead_one_still_fails(self) -> None:
        """One dead scenario is enough, even among passing ones."""
        dead = ScenarioResult(
            scenario=_scenario("onap"),
            container_started=False,
            error="container did not start",
        )

        assert print_summary([_healthy_result(), dead], []) == 1

    def test_failed_tunnel_test_exits_non_zero(self) -> None:
        """Tunnel results are still counted."""
        tunnel = [HarnessTestResult(name="tunnel_inputs", passed=False)]

        assert print_summary([_healthy_result()], tunnel) == 1

    def test_startup_failure_reported_separately(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The summary says the scenario failed before testing."""
        dead = ScenarioResult(
            scenario=_scenario("onap"),
            container_started=False,
            error="container did not start",
        )

        with caplog.at_level("INFO", logger="replharness_report"):
            print_summary([dead], [])

        messages = [r.getMessage() for r in caplog.records]
        assert any("failed before testing" in m for m in messages)
        assert not any("ALL TESTS PASSED" in m for m in messages)
