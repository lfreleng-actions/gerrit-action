# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the local replication harness reporting.

``scripts/test-replication-local.py`` is a developer harness rather
than an action entry point, but its summary decides the exit code a
developer reads a replication change through, so the aggregation is
worth pinning down.
"""

from __future__ import annotations

import importlib.util
import itertools
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from errors import DockerError
from replharness_checks import _test_steady_state_detection
from replharness_cli import install_cleanup_handler, resolve_harness_config
from replharness_container import _start_container

# ``TestResult`` is aliased because pytest would otherwise try to
# collect the harness's result record as a test class.
from replharness_model import (
    Scenario,
    ScenarioResult,
    ScenarioRunOptions,
    _ContainerContext,
)
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


class _FakeClock:
    """A controllable stand-in for ``time.time``."""

    def __init__(self, start: float = 1000.0) -> None:
        """Start the clock at *start* seconds."""
        self.now = start

    def time(self) -> float:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by *seconds*."""
        self.now += seconds


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
        """A tracker that reaches stability yields a pass.

        Quiet time is credited from post-collection observation, so
        the clock has to actually advance across samples for the
        window to be satisfied.
        """
        clock = _FakeClock()
        snap = ReplicationSnapshot(
            timestamp=clock.now,
            completed_count=10,
            disk_usage_kb=5000,
            log_line_count=200,
            repo_count=10,
        )

        def _sleep(seconds: float) -> None:
            """Advance the fake clock instead of waiting."""
            clock.advance(seconds)

        with (
            patch("replharness_checks.take_snapshot", return_value=snap),
            patch("replharness_checks.time.sleep", side_effect=_sleep),
            patch("replharness_checks.time.time", clock.time),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        assert result.passed is True
        assert "stable=True" in result.message

    def test_slow_first_snapshot_is_not_credited(self) -> None:
        """Quiet time is measured from observation, not from collection.

        A first snapshot timestamped at t=0 whose Docker queries only
        finish at t=40, followed by an identical one at t=50, has been
        observed unchanged for 10 seconds — not 50.
        """
        clock = _FakeClock()
        snap = ReplicationSnapshot(
            timestamp=clock.now,
            completed_count=10,
            disk_usage_kb=5000,
            log_line_count=200,
            repo_count=10,
        )
        collections = itertools.count()

        def _sleep(seconds: float) -> None:
            """Advance the fake clock instead of waiting."""
            clock.advance(seconds)

        def _slow_first(*_args: object, **_kwargs: object) -> ReplicationSnapshot:
            """Take 40s over the first collection, then be quick."""
            clock.advance(40 if next(collections) == 0 else 0)
            return snap

        with (
            patch("replharness_checks.take_snapshot", side_effect=_slow_first),
            patch("replharness_checks.time.sleep", side_effect=_sleep),
            patch("replharness_checks.time.time", clock.time),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        # Passing at 50s would mean crediting the 40s spent collecting
        # the first snapshot, which nobody watched.  The check waits
        # until a full window of quiet has actually been observed.
        assert result.passed is True
        assert "after 70s" in result.message

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

    def test_single_slow_snapshot_is_not_stable(self) -> None:
        """One stale snapshot must not be read as quiescence.

        The check credits quiet time from its own post-collection
        observations rather than from snapshot timestamps, so a
        backdated snapshot cannot buy a pass. This injects one to
        prove that independence holds: a single sample, however old it
        claims to be, is not a repeated observation.
        """
        stale = ReplicationSnapshot(
            timestamp=time.time() - 600,
            completed_count=10,
            disk_usage_kb=5000,
            log_line_count=200,
            repo_count=10,
        )
        snapshots: list[ReplicationSnapshot] = []

        def _one_stale_then_moving(
            *_args: object, **_kwargs: object
        ) -> ReplicationSnapshot:
            """Return the stale snapshot once, then changing state."""
            snapshots.append(stale)
            if len(snapshots) == 1:
                return stale
            n = len(snapshots)
            return ReplicationSnapshot(
                timestamp=time.time(),
                completed_count=n,
                disk_usage_kb=1000 * n,
                log_line_count=10 * n,
                repo_count=n,
            )

        with (
            patch(
                "replharness_checks.take_snapshot",
                side_effect=_one_stale_then_moving,
            ),
            patch("replharness_checks.time.sleep"),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        # More than one sample was taken, and the stale first one did
        # not short-circuit the check into a pass.
        assert len(snapshots) > 1
        assert result.passed is False

    def test_budget_bounds_wall_clock_not_just_samples(self) -> None:
        """Slow snapshots must not overrun the advertised budget.

        Each sample issues four Docker calls. Terminating on sample
        count alone let a sluggish connection run the check for far
        longer than the budget it reports in its own failure message.
        """
        clock = _FakeClock()
        sleeps: list[float] = []
        counter = itertools.count(1)

        def _sleep(seconds: float) -> None:
            """Advance the fake clock instead of waiting."""
            sleeps.append(seconds)
            clock.advance(seconds)

        def _slow_moving_snapshot(
            *_args: object, **_kwargs: object
        ) -> ReplicationSnapshot:
            """Take 60s per sample and never settle."""
            clock.advance(60)
            n = next(counter)
            return ReplicationSnapshot(
                timestamp=clock.now,
                completed_count=n,
                disk_usage_kb=1000 * n,
                log_line_count=10 * n,
                repo_count=n,
            )

        with (
            patch(
                "replharness_checks.take_snapshot", side_effect=_slow_moving_snapshot
            ),
            patch("replharness_checks.time.sleep", side_effect=_sleep),
            patch("replharness_checks.time.time", clock.time),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        assert result.passed is False
        # Budget is 3 x 30s and each sample costs ~70s of clock, so the
        # loop stops after a single interval instead of running the
        # nine samples the count-based bound alone would have allowed.
        assert len(sleeps) == 1

    def test_slow_changed_sample_is_not_stable(self) -> None:
        """A sample that changed cannot be credited as stable.

        Snapshots are fed in already stale, so `_StabilityTracker`
        alone would report quiescence: each arrives claiming to be
        older than the window. The check must refuse regardless,
        because the state changed between the only two observations
        it actually made.
        """
        clock = _FakeClock()
        counter = itertools.count(1)

        def _sleep(seconds: float) -> None:
            """Advance the fake clock instead of waiting."""
            clock.advance(seconds)

        def _slow_changing_snapshot(
            *_args: object, **_kwargs: object
        ) -> ReplicationSnapshot:
            """Change on every sample, and take 40s to collect."""
            timestamp = clock.now
            clock.advance(40)
            n = next(counter)
            return ReplicationSnapshot(
                timestamp=timestamp,
                completed_count=n,
                disk_usage_kb=1000 * n,
                log_line_count=10 * n,
                repo_count=n,
            )

        with (
            patch(
                "replharness_checks.take_snapshot",
                side_effect=_slow_changing_snapshot,
            ),
            patch("replharness_checks.time.sleep", side_effect=_sleep),
            patch("replharness_checks.time.time", clock.time),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        # The tracker alone would have said "stable" here, because each
        # snapshot is 40s old on arrival against a 30s window.
        assert result.passed is False

    def test_stability_not_credited_past_the_budget(self) -> None:
        """A pass must not be reported after the budget is spent."""
        clock = _FakeClock()
        # Unchanging state, but every sample is slow enough that the
        # budget expires before the check can be satisfied.
        snap = ReplicationSnapshot(
            timestamp=clock.now,
            completed_count=10,
            disk_usage_kb=5000,
            log_line_count=200,
            repo_count=10,
        )

        def _sleep(seconds: float) -> None:
            """Advance the fake clock instead of waiting."""
            clock.advance(seconds)

        def _slow_same_snapshot(
            *_args: object, **_kwargs: object
        ) -> ReplicationSnapshot:
            """Return identical state, 100s per collection."""
            clock.advance(100)
            return snap

        with (
            patch("replharness_checks.take_snapshot", side_effect=_slow_same_snapshot),
            patch("replharness_checks.time.sleep", side_effect=_sleep),
            patch("replharness_checks.time.time", clock.time),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=30
            )

        assert result.passed is False
        assert "budget" in result.message

    def test_short_window_can_still_pass(self) -> None:
        """A one-second window must remain satisfiable.

        The sampling interval had a flat five-second floor, so a single
        interval already exceeded the three-second budget for
        STABILITY_WINDOW=1 and a quiescent instance was always
        reported as unstable.
        """
        clock = _FakeClock()
        snap = ReplicationSnapshot(
            timestamp=clock.now,
            completed_count=10,
            disk_usage_kb=5000,
            log_line_count=200,
            repo_count=10,
        )

        def _sleep(seconds: float) -> None:
            """Advance the fake clock instead of waiting."""
            clock.advance(seconds)

        with (
            patch("replharness_checks.take_snapshot", return_value=snap),
            patch("replharness_checks.time.sleep", side_effect=_sleep),
            patch("replharness_checks.time.time", clock.time),
        ):
            result = _test_steady_state_detection(
                MagicMock(), "abc123", _scenario(), stability_window=1
            )

        assert result.passed is True

    @pytest.mark.parametrize("window", [0, -5])
    def test_degenerate_window_fails_without_crashing(self, window: int) -> None:
        """A nonsensical window must fail the check, not the harness.

        STABILITY_WINDOW is read from the environment as an
        unrestricted integer, so zero reaches the sampling arithmetic
        and used to raise ZeroDivisionError — killing the run before
        it could print its summary.
        """
        snap = ReplicationSnapshot(
            timestamp=1000.0,
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
                MagicMock(), "abc123", _scenario(), stability_window=window
            )

        assert result.passed is False


def _load_harness_entry_point() -> Any:
    """Load ``scripts/test-replication-local.py`` as a module.

    The harness entry point is a hyphenated script rather than a
    package, so it is loaded the same way ``test_verify_tunnel`` and
    ``test_start_instances`` load theirs.
    """
    script = Path(__file__).resolve().parent.parent / "scripts"
    script = script / "test-replication-local.py"
    spec = importlib.util.spec_from_file_location("replharness_entry", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _container_context() -> _ContainerContext:
    """Build a container record for the interrupt-registry tests."""
    return _ContainerContext(
        cid="abc123",
        name="gerrit-test-lf",
        http_port=18080,
        ssh_port=39418,
        work_dir=Path("/tmp/gerrit-test-lf"),
    )


def _run_options() -> ScenarioRunOptions:
    """Build the run-level options a scenario needs."""
    return ScenarioRunOptions(
        image="gerrit:test",
        creds=("user", "pass"),
        timeout=60,
        stability_window=30,
        fetch_every="15s",
    )


class TestInterruptCleanup:
    """Containers must actually be removed on Ctrl-C."""

    def test_handler_removes_registered_containers(self) -> None:
        """The handler drains whatever the caller registered."""
        docker = MagicMock()
        handlers: list[Any] = []

        with patch(
            "replharness_cli.signal.signal",
            side_effect=lambda _sig, handler: handlers.append(handler),
        ):
            tracked = install_cleanup_handler(docker)

        ctx = _container_context()
        tracked.append(ctx)

        with (
            patch("replharness_cli._cleanup_container") as mock_cleanup,
            pytest.raises(SystemExit) as excinfo,
        ):
            handlers[0](signal.SIGINT, None)

        assert excinfo.value.code == 130
        mock_cleanup.assert_called_once_with(docker, ctx)

    def test_handler_reports_when_nothing_to_clean(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With nothing registered it must not claim to have cleaned up."""
        handlers: list[Any] = []

        with patch(
            "replharness_cli.signal.signal",
            side_effect=lambda _sig, handler: handlers.append(handler),
        ):
            install_cleanup_handler(MagicMock())

        with (
            caplog.at_level("INFO", logger="replharness_cli"),
            pytest.raises(SystemExit),
        ):
            handlers[0](signal.SIGINT, None)

        messages = [r.getMessage() for r in caplog.records]
        assert any("no containers to clean up" in m for m in messages)

    def test_scenario_registers_and_deregisters_container(self) -> None:
        """run_scenario tracks its container only while it is running.

        The entry point used to discard the list install_cleanup_handler
        returns, so nothing was ever registered and Ctrl-C exited 130
        while leaving every container behind.
        """
        harness = _load_harness_entry_point()
        ctx = _container_context()
        tracked: list[_ContainerContext] = []
        seen_during_run: list[list[_ContainerContext]] = []

        def _record_checks(*_args: Any, **_kwargs: Any) -> None:
            """Capture the registry contents mid-scenario."""
            seen_during_run.append(list(tracked))

        def _start(*_args: Any, **kwargs: Any) -> _ContainerContext:
            """Register the container the way the real starter does."""
            kwargs["tracked"].append(ctx)
            return ctx

        with (
            patch.object(harness, "_start_container", side_effect=_start),
            patch.object(harness, "await_gerrit_ready", return_value=""),
            patch.object(harness, "wait_initial_cycle"),
            patch.object(harness, "run_scenario_checks", side_effect=_record_checks),
            patch.object(harness, "release_container") as mock_release,
        ):
            harness.run_scenario(MagicMock(), _scenario(), 0, _run_options(), tracked)

        # Registered while the container was up…
        assert seen_during_run == [[ctx]]
        # …and deregistered once released, so a later interrupt does
        # not try to remove an already-removed container.
        assert tracked == []
        mock_release.assert_called_once()

    def test_container_deregistered_when_scenario_fails(self) -> None:
        """A scenario that dies still leaves the registry clean."""
        harness = _load_harness_entry_point()
        ctx = _container_context()
        tracked: list[_ContainerContext] = []

        def _start(*_args: Any, **kwargs: Any) -> _ContainerContext:
            """Register the container the way the real starter does."""
            kwargs["tracked"].append(ctx)
            return ctx

        with (
            patch.object(harness, "_start_container", side_effect=_start),
            patch.object(
                harness,
                "await_gerrit_ready",
                return_value="Gerrit did not become ready within 120s",
            ),
            patch.object(harness, "release_container"),
        ):
            result = harness.run_scenario(
                MagicMock(), _scenario(), 0, _run_options(), tracked
            )

        assert result.gerrit_ready is False
        assert tracked == []

    def test_interrupt_mid_scenario_cleans_up_once(self) -> None:
        """An interrupt inside a scenario must not double-clean.

        The handler calls sys.exit(130), so SystemExit unwinds through
        run_scenario's finally block. If that block released the
        container unconditionally it would tear the same container
        down twice — or, under --keep, announce that a container the
        handler had just removed was still running.
        """
        harness = _load_harness_entry_point()
        ctx = _container_context()
        docker = MagicMock()
        handlers: list[Any] = []

        with patch(
            "replharness_cli.signal.signal",
            side_effect=lambda _sig, handler: handlers.append(handler),
        ):
            tracked = install_cleanup_handler(docker)

        def _interrupt(*_args: Any, **_kwargs: Any) -> None:
            """Deliver the interrupt part-way through the scenario."""
            handlers[0](signal.SIGINT, None)

        def _start(*_args: Any, **kwargs: Any) -> _ContainerContext:
            """Register the container the way the real starter does."""
            kwargs["tracked"].append(ctx)
            return ctx

        with (
            patch.object(harness, "_start_container", side_effect=_start),
            patch.object(harness, "await_gerrit_ready", return_value=""),
            patch.object(harness, "wait_initial_cycle", side_effect=_interrupt),
            patch.object(harness, "release_container") as mock_release,
            patch("replharness_cli._cleanup_container") as mock_cleanup,
            pytest.raises(SystemExit) as excinfo,
        ):
            harness.run_scenario(docker, _scenario(), 0, _run_options(), tracked)

        assert excinfo.value.code == 130
        # The handler removed it exactly once…
        mock_cleanup.assert_called_once_with(docker, ctx)
        # …and the unwinding finally block left it alone.
        mock_release.assert_not_called()
        assert tracked == []

    def test_interrupt_during_container_start_cleans_up(self) -> None:
        """A container created by an interrupted `docker run` is removed.

        Registration used to happen only after `_start_container`
        returned, so an interrupt while `docker run -d` was executing —
        after Docker created the container but before the ID came
        back — left the handler with an empty registry and leaked it.
        """
        docker = MagicMock()
        handlers: list[Any] = []

        with patch(
            "replharness_cli.signal.signal",
            side_effect=lambda _sig, handler: handlers.append(handler),
        ):
            tracked = install_cleanup_handler(docker)

        def _interrupt_during_run(*_args: Any, **_kwargs: Any) -> None:
            """Deliver the interrupt from inside `docker run`."""
            handlers[0](signal.SIGINT, None)

        docker.run_cmd.side_effect = _interrupt_during_run

        with (
            patch("replharness_cli._cleanup_container") as mock_handler_cleanup,
            patch("replharness_container._cleanup_container") as mock_final_cleanup,
            pytest.raises(SystemExit) as excinfo,
        ):
            _start_container(
                docker,
                _scenario(),
                "gerrit:test",
                0,
                ("user", "pass"),
                tracked=tracked,
            )

        assert excinfo.value.code == 130
        # The provisional entry was actionable: cleanup ran against the
        # container name, which `docker rm -f` accepts.
        mock_handler_cleanup.assert_called_once()
        cleaned = mock_handler_cleanup.call_args.args[1]
        assert cleaned.name.startswith("gerrit-test-lf-small")
        assert cleaned.cid == cleaned.name
        # And the unwinding start-up path removes it by name again, in
        # case the daemon created it after the CLI died.
        mock_final_cleanup.assert_called_once()
        assert mock_final_cleanup.call_args.args[1].name == cleaned.name
        assert tracked == []

    def test_failed_run_cleans_up_by_name(self) -> None:
        """A failed `docker run` still attempts a name-based removal."""
        docker = MagicMock()
        docker.run_cmd.side_effect = DockerError("no such image")
        tracked: list[_ContainerContext] = []

        with (
            patch("replharness_container._cleanup_container") as mock_cleanup,
            pytest.raises(DockerError),
        ):
            _start_container(
                docker,
                _scenario(),
                "gerrit:test",
                0,
                ("user", "pass"),
                tracked=tracked,
            )

        mock_cleanup.assert_called_once()
        assert tracked == []

    def test_interrupt_while_writing_configs_cleans_up(self) -> None:
        """Credentials must not be left in /tmp by an interrupt.

        The work directory is created and `secure.config` written
        before `docker run` is reached. Registering only after that
        would leave the password on disk if the run was interrupted
        during preparation — and because the run token makes the path
        unique, no later run would reuse or clean it.
        """
        docker = MagicMock()
        tracked: list[_ContainerContext] = []

        with (
            patch(
                "replharness_container._write_instance_config",
                side_effect=KeyboardInterrupt,
            ),
            patch("replharness_container._cleanup_container") as mock_cleanup,
            pytest.raises(KeyboardInterrupt),
        ):
            _start_container(
                docker,
                _scenario(),
                "gerrit:test",
                0,
                ("user", "pass"),
                tracked=tracked,
            )

        # Cleanup ran against the run's own directory, which is what
        # removes the credentials written into it.
        mock_cleanup.assert_called_once()
        cleaned = mock_cleanup.call_args.args[1]
        assert str(cleaned.work_dir).startswith("/tmp/gerrit-test-lf-small-")
        assert tracked == []
        # The container was never started.
        docker.run_cmd.assert_not_called()

    def test_started_container_registered_by_id(self) -> None:
        """On success the registry holds the real container ID."""
        docker = MagicMock()
        docker.run_cmd.return_value = SimpleNamespace(stdout="deadbeefcafe\n")
        tracked: list[_ContainerContext] = []

        ctx = _start_container(
            docker,
            _scenario(),
            "gerrit:test",
            0,
            ("user", "pass"),
            tracked=tracked,
        )

        assert ctx.cid == "deadbeefcafe"
        # Exactly one entry, and the placeholder is gone.
        assert tracked == [ctx]


class TestScenarioOutcome:
    """A recorded error must override otherwise-passing tests."""

    def test_error_after_readiness_fails_the_scenario(self) -> None:
        """An exception part-way through the checks is not a pass.

        run_scenario records the reason in `error` and leaves the
        results collected so far in place, so counting only the test
        outcomes would keep the green icon for a scenario that never
        finished.
        """
        result = _healthy_result()
        result.error = "Unexpected error: docker exec timed out"

        assert result.passed is False
        assert print_summary([result], []) == 1

    def test_error_with_no_tests_still_fails(self) -> None:
        """The same holds when the failure precedes every check."""
        result = ScenarioResult(
            scenario=_scenario(),
            container_started=True,
            gerrit_ready=True,
            error="Unexpected error: connection reset",
        )

        assert result.passed is False
        assert print_summary([result], []) == 1


class TestHarnessConfig:
    """Environment values the harness cannot act on must be rejected."""

    ENV_VARS = (
        "GERRIT_VERSION",
        "PLUGIN_VERSION",
        "REPLICATION_WAIT_TIMEOUT",
        "STABILITY_WINDOW",
        "FETCH_EVERY",
    )

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the host environment out of these tests."""
        for name in self.ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_defaults_are_valid(self) -> None:
        """Every documented default passes validation."""
        config = resolve_harness_config()

        assert config.timeout == 180
        assert config.stability_window == 30
        assert config.fetch_every == "15s"

    def test_valid_overrides_are_respected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legitimate values pass through unchanged."""
        monkeypatch.setenv("REPLICATION_WAIT_TIMEOUT", "120")
        monkeypatch.setenv("STABILITY_WINDOW", "20")
        monkeypatch.setenv("FETCH_EVERY", "5m")

        config = resolve_harness_config()

        assert config.timeout == 120
        assert config.stability_window == 20
        assert config.fetch_every == "5m"

    @pytest.mark.parametrize("name", ["REPLICATION_WAIT_TIMEOUT", "STABILITY_WINDOW"])
    @pytest.mark.parametrize("value", ["3O", "", "12.5", "abc"])
    def test_non_numeric_is_rejected_by_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        name: str,
        value: str,
    ) -> None:
        """A typo fails with the variable named, not a traceback."""
        monkeypatch.setenv(name, value)

        with (
            caplog.at_level("ERROR", logger="replharness_cli"),
            pytest.raises(SystemExit) as excinfo,
        ):
            resolve_harness_config()

        assert excinfo.value.code == 1
        assert any(name in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("name", ["REPLICATION_WAIT_TIMEOUT", "STABILITY_WINDOW"])
    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_non_positive_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, name: str, value: str
    ) -> None:
        """Zero and negatives reach the timing arithmetic otherwise.

        STABILITY_WINDOW=0 previously divided by zero inside the
        steady-state check, killing the run before its summary.
        """
        monkeypatch.setenv(name, value)

        with pytest.raises(SystemExit) as excinfo:
            resolve_harness_config()

        assert excinfo.value.code == 1

    @pytest.mark.parametrize("value", ["15sec", "soon", "-1s", "0s"])
    def test_bad_fetch_every_fails_before_any_container(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        value: str,
    ) -> None:
        """FETCH_EVERY was previously parsed only after a container ran.

        `wait_initial_cycle` calls `parse_interval_to_seconds` once the
        scenario is under way, so a typo cost a full image build and
        container start before surfacing.
        """
        monkeypatch.setenv("FETCH_EVERY", value)

        with (
            caplog.at_level("ERROR", logger="replharness_cli"),
            pytest.raises(SystemExit) as excinfo,
        ):
            resolve_harness_config()

        assert excinfo.value.code == 1
        assert any("FETCH_EVERY" in r.getMessage() for r in caplog.records)


class TestTunnelOnlyEntryPoint:
    """Tunnel checks must not depend on scenario tuning values."""

    def test_tunnel_only_ignores_invalid_scenario_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unusable FETCH_EVERY must not block a tunnel check.

        The tunnel tests are container-free and use none of the
        scenario knobs, so resolving (and validating) those before the
        tunnel-only early return would fail a run that never needed
        them.
        """
        harness = _load_harness_entry_point()
        monkeypatch.setenv("FETCH_EVERY", "not-an-interval")
        monkeypatch.setenv("STABILITY_WINDOW", "0")

        args = SimpleNamespace(
            list=False,
            tunnel_only=True,
            scenario=None,
            keep=False,
            skip_build=False,
        )

        with (
            patch.object(harness, "parse_args", return_value=args),
            patch.object(harness, "setup_logging"),
            patch.object(harness, "run_tunnel_tests", return_value=[]) as mock_tunnel,
            patch.object(harness, "resolve_harness_config") as mock_config,
        ):
            exit_code = harness.main()

        assert exit_code == 0
        mock_tunnel.assert_called_once()
        # The scenario configuration is never resolved on this path.
        mock_config.assert_not_called()
