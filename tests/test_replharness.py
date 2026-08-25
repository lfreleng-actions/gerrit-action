# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the local replication harness reporting.

``scripts/test-replication-local.py`` is a developer harness rather
than an action entry point, but its summary decides the exit code a
developer reads a replication change through, so the aggregation is
worth pinning down.
"""

from __future__ import annotations

import itertools
import time
from unittest.mock import MagicMock, patch

import pytest
from replharness_checks import _test_steady_state_detection

# ``TestResult`` is aliased because pytest would otherwise try to
# collect the harness's result record as a test class.
from replharness_model import Scenario, ScenarioResult
from replharness_model import TestResult as HarnessTestResult
from replharness_report import print_summary
from replication_model import ReplicationSnapshot


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


class TestSteadyStateDetection:
    """The steady-state check must be able to report a failure."""

    def test_stable_state_passes(self) -> None:
        """A tracker that reaches stability yields a pass."""
        # A snapshot timestamped a full window in the past makes the
        # tracker report stable on the first query.
        snap = ReplicationSnapshot(
            timestamp=time.time() - 120,
            completed_count=10,
            disk_usage_kb=5000,
            log_line_count=200,
            repo_count=10,
        )

        with (
            patch("replharness_checks.take_snapshot", return_value=snap),
            patch("replharness_checks.time.sleep"),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        assert result.passed is True
        assert "stable=True" in result.message

    def test_state_still_changing_fails(self) -> None:
        """State that never settles now fails instead of passing.

        The check previously returned passed=True on both branches, so
        it could not report a regression in steady-state detection.
        """
        counter = itertools.count(1)

        def _moving_snapshot(*_args: object, **_kwargs: object) -> ReplicationSnapshot:
            """Return a snapshot that differs on every call."""
            n = next(counter)
            return ReplicationSnapshot(
                timestamp=time.time(),
                completed_count=n,
                disk_usage_kb=1000 * n,
                log_line_count=10 * n,
                repo_count=n,
            )

        with (
            patch("replharness_checks.take_snapshot", side_effect=_moving_snapshot),
            patch("replharness_checks.time.sleep") as mock_sleep,
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        assert result.passed is False
        assert "stable=False" in result.message
        # Bounded: it samples across the budget rather than forever.
        # budget = 3 × 30s, sampled every max(30 // 3, 5) = 10s.
        assert mock_sleep.call_count == 9
        assert mock_sleep.call_args_list[0].args[0] == 10
