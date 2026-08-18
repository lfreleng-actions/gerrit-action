# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Scenario orchestration for the local replication test harness.

Owns the *sequence* a scenario goes through — start a container, wait
for Gerrit, give pull-replication one fetch cycle, run every check,
then always tear down (unless ``--keep``) — and the loop that repeats
it across a selection of scenarios.

Checks are appended to the scenario's result as they complete rather
than collected and assigned at the end, so a scenario that blows up
half-way still reports everything that had already been established.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any

from config import parse_interval_to_seconds
from docker_manager import DockerManager
from errors import DockerError
from harness_checks import (
    check_content_threshold,
    check_log_line_count,
    check_no_false_errors,
    check_snapshot_fields,
    check_steady_state_detection,
    check_wait_for_replication,
)
from harness_config import HarnessSettings, resolve_credentials
from harness_containers import (
    ContainerContext,
    cleanup_container,
    start_container,
    wait_for_gerrit_ready,
)
from harness_results import CheckResult, ScenarioResult
from harness_scenarios import Scenario

logger = logging.getLogger(__name__)


def run_scenario(
    docker: DockerManager,
    scenario: Scenario,
    index: int,
    *,
    image: str,
    creds: tuple[str, str],
    settings: HarnessSettings,
    keep: bool = False,
) -> ScenarioResult:
    """Execute all tests for a single scenario."""
    result = ScenarioResult(scenario=scenario)

    _log_scenario_header(scenario, settings)

    ctx: ContainerContext | None = None
    try:
        # Start container
        ctx = start_container(
            docker,
            scenario,
            image,
            index,
            creds,
            fetch_every=settings.fetch_every,
        )
        result.container_started = True

        # Wait for Gerrit to be ready
        if not _await_gerrit_ready(docker, ctx, result):
            return result

        # Give pull-replication time for the first fetch cycle
        _await_initial_cycle(settings.fetch_every)

        # --- Run tests ---
        logger.info("")
        logger.info("  Running tests…")
        logger.info("")

        _append_scenario_checks(docker, ctx.cid, scenario, settings, result.tests)

    except Exception as exc:
        result.error = f"Unexpected error: {exc}"
        logger.exception("  %s", result.error)

    finally:
        _finish_container(docker, ctx, keep=keep)

    return result


def _log_scenario_header(scenario: Scenario, settings: HarnessSettings) -> None:
    """Log the banner describing the scenario about to run."""
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
    logger.info("  Timeout:  %ds", settings.timeout)
    logger.info("  Stability window: %ds", settings.stability_window)
    logger.info("")


def _await_gerrit_ready(
    docker: DockerManager, ctx: ContainerContext, result: ScenarioResult
) -> bool:
    """Wait for Gerrit readiness, recording the outcome on *result*.

    On failure the tail of the container log is dumped, since that is
    the only evidence available once the container is torn down.
    """
    logger.info("  Waiting for Gerrit to start…")
    ready_timeout = 120
    if wait_for_gerrit_ready(docker, ctx.cid, timeout=ready_timeout):
        result.gerrit_ready = True
        logger.info("  Gerrit ready ✅")
        return True

    result.error = f"Gerrit did not become ready within {ready_timeout}s"
    logger.error("  %s", result.error)
    # Dump logs for debugging
    try:
        logs = docker.container_logs(ctx.cid, tail=50)
        for line in logs.splitlines()[-20:]:
            logger.error("    %s", line.strip())
    except DockerError as exc:
        logger.debug("Could not dump container logs: %s", exc)
    return False


def _await_initial_cycle(fetch_every: str) -> None:
    """Sleep long enough for pull-replication's first fetch cycle."""
    fetch_secs = parse_interval_to_seconds(fetch_every)
    initial_wait = max(fetch_secs + 10, 30)
    logger.info(
        "  Waiting %ds for initial replication cycle (fetchEvery=%s)…",
        initial_wait,
        fetch_every,
    )
    time.sleep(initial_wait)


def _append_scenario_checks(
    docker: DockerManager,
    cid: str,
    scenario: Scenario,
    settings: HarnessSettings,
    results: list[CheckResult],
) -> None:
    """Run every container check in order, appending as each completes.

    Appending in place (rather than returning a list) keeps the results
    already gathered visible to the caller if a later check raises.
    """
    # 1. Snapshot fields
    results.append(check_snapshot_fields(docker, cid))

    # 2. Log line count
    results.append(check_log_line_count(docker, cid))

    # 3. No false error detection
    results.append(check_no_false_errors(docker, cid))

    # 4. Content threshold (regression test for 86MB/36-repo bug)
    results.append(check_content_threshold(docker, cid, scenario))

    # 5. Steady-state detection
    results.append(
        check_steady_state_detection(docker, cid, scenario, settings.stability_window)
    )

    # 6. Full wait_for_replication — this is the integration test
    results.append(
        check_wait_for_replication(
            docker, cid, scenario, settings.timeout, settings.stability_window
        )
    )


def _finish_container(
    docker: DockerManager, ctx: ContainerContext | None, *, keep: bool
) -> None:
    """Tear the container down, or report how to reach it if kept."""
    if ctx and not keep:
        logger.info("")
        logger.info("  Cleaning up container %s…", ctx.name)
        cleanup_container(docker, ctx)
    elif ctx and keep:
        logger.info("")
        logger.info(
            "  Container kept running (--keep): %s  "
            "http://localhost:%d  ssh://localhost:%d",
            ctx.name,
            ctx.http_port,
            ctx.ssh_port,
        )


def run_all_scenarios(
    docker: DockerManager,
    scenarios: list[Scenario],
    *,
    image: str,
    settings: HarnessSettings,
    keep: bool,
) -> list[ScenarioResult]:
    """Run every selected scenario and collect the results."""
    scenario_results: list[ScenarioResult] = []

    # Install signal handler for clean shutdown
    _shutdown_contexts: list[ContainerContext] = []

    def _signal_handler(_signum: int, _frame: Any) -> None:
        logger.info("\nInterrupted — cleaning up…")
        for ctx in _shutdown_contexts:
            cleanup_container(docker, ctx)
        sys.exit(130)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    for idx, scenario in enumerate(scenarios):
        # Check credentials before starting container
        try:
            creds = resolve_credentials(scenario.gerrit_host)
        except SystemExit:
            sr = ScenarioResult(scenario=scenario)
            sr.error = f"No credentials for {scenario.gerrit_host}"
            scenario_results.append(sr)
            continue

        sr = run_scenario(
            docker,
            scenario,
            idx,
            image=image,
            creds=creds,
            settings=settings,
            keep=keep,
        )
        scenario_results.append(sr)

    return scenario_results
