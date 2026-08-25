#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Local Docker test harness for replication verification.

Exercises the replication detection improvements across multiple Gerrit
project configurations **locally in Docker**, without requiring GitHub
Actions.  Each test scenario spins up a real Gerrit container, configures
pull-replication against a public upstream, and validates that:

1. The content-size threshold does not cause false negatives for small
   repos (the original 86 MB / 36-repo bug).
2. Steady-state detection terminates the wait loop early instead of
   blocking for the full timeout.
3. Transient errors do not cause premature failure.
4. Progress reporting shows *why* the loop is still waiting.
5. The tunnel verification script produces actionable diagnostics.

The individual pieces live in focused modules under ``scripts/lib`` and
are re-exported here, so this file stays the single entry point:
:mod:`replharness_model` (scenario catalogue, result records and option
bundles), :mod:`replharness_container` (image, container lifecycle and
credentials), :mod:`replharness_checks` (per-scenario assertions),
:mod:`replharness_tunnel` (container-free tunnel checks),
:mod:`replharness_report` (progress banners and the final summary) and
:mod:`replharness_cli` (argument parsing and run set-up).

Usage::

    # Run all scenarios (needs Docker + network access + ~/.netrc creds)
    python scripts/test-replication-local.py

    # Run a single scenario by name
    python scripts/test-replication-local.py --scenario lf-small

    # List available scenarios without running them
    python scripts/test-replication-local.py --list

    # Use explicit credentials instead of ~/.netrc
    GERRIT_HTTP_USERNAME=user GERRIT_HTTP_PASSWORD=pass \\
        python scripts/test-replication-local.py

    # Override timeouts for faster iteration
    REPLICATION_WAIT_TIMEOUT=120 STABILITY_WINDOW=20 \\
        python scripts/test-replication-local.py --scenario lf-small

    # Keep containers running after test for manual inspection
    python scripts/test-replication-local.py --keep

Environment Variables
---------------------
GERRIT_HTTP_USERNAME / GERRIT_HTTP_PASSWORD
    HTTP Basic auth credentials.  Falls back to ``~/.netrc`` entries.
GERRIT_VERSION
    Gerrit Docker image tag (default: ``3.13.1-ubuntu24``).
PLUGIN_VERSION
    Pull-replication plugin branch (default: ``stable-3.13``).
REPLICATION_WAIT_TIMEOUT
    Per-scenario timeout in seconds (default: ``180``).
STABILITY_WINDOW
    Seconds of no-change before declaring stable (default: ``30``).
FETCH_EVERY
    Poll interval for pull-replication (default: ``15s``).
DEBUG
    ``"true"`` for verbose output.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from docker_manager import DockerManager  # noqa: E402
from logging_utils import setup_logging  # noqa: E402
from replharness_checks import (  # noqa: E402
    _test_content_threshold,  # noqa: F401
    _test_log_line_count,  # noqa: F401
    _test_no_false_errors,  # noqa: F401
    _test_snapshot_fields,  # noqa: F401
    _test_steady_state_detection,  # noqa: F401
    _test_wait_for_replication,  # noqa: F401
    run_scenario_checks,
)
from replharness_cli import (  # noqa: E402
    install_cleanup_handler,
    list_scenarios,
    log_harness_config,
    parse_args,
    resolve_harness_config,
    resolve_image,
    select_scenarios,
)
from replharness_container import (  # noqa: E402
    _build_image,  # noqa: F401
    _cleanup_container,  # noqa: F401
    _get_credentials,
    _start_container,
    _wait_for_gerrit_ready,  # noqa: F401
    await_gerrit_ready,
    release_container,
    wait_initial_cycle,
)
from replharness_model import (  # noqa: E402
    _BASE_HTTP_PORT,  # noqa: F401
    _BASE_SSH_PORT,  # noqa: F401
    _SCENARIO_MAP,  # noqa: F401
    SCENARIOS,  # noqa: F401
    HarnessConfig,  # noqa: F401
    Scenario,
    ScenarioResult,
    ScenarioRunOptions,
    TestResult,  # noqa: F401
    _ContainerContext,
)
from replharness_report import log_scenario_banner, print_summary  # noqa: E402
from replharness_tunnel import (  # noqa: E402
    _test_tunnel_script_handles_unreachable,  # noqa: F401
    _test_tunnel_script_validates_inputs,  # noqa: F401
    run_tunnel_tests,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SCENARIOS",
    "HarnessConfig",
    "Scenario",
    "ScenarioResult",
    "ScenarioRunOptions",
    "TestResult",
    # The underscore-prefixed entries are internals of this harness that
    # moved into the sibling modules; they are listed so the re-export
    # stays explicit.
    "_BASE_HTTP_PORT",
    "_BASE_SSH_PORT",
    "_SCENARIO_MAP",
    "_ContainerContext",
    "_build_image",
    "_cleanup_container",
    "_get_credentials",
    "_start_container",
    "_test_content_threshold",
    "_test_log_line_count",
    "_test_no_false_errors",
    "_test_snapshot_fields",
    "_test_steady_state_detection",
    "_test_tunnel_script_handles_unreachable",
    "_test_tunnel_script_validates_inputs",
    "_test_wait_for_replication",
    "_wait_for_gerrit_ready",
    "await_gerrit_ready",
    "install_cleanup_handler",
    "list_scenarios",
    "log_harness_config",
    "log_scenario_banner",
    "main",
    "parse_args",
    "print_summary",
    "release_container",
    "resolve_harness_config",
    "resolve_image",
    "run_scenario",
    "run_scenario_checks",
    "run_tunnel_tests",
    "select_scenarios",
    "wait_initial_cycle",
]


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


def run_scenario(
    docker: DockerManager,
    scenario: Scenario,
    index: int,
    options: ScenarioRunOptions,
    tracked: list[_ContainerContext] | None = None,
) -> ScenarioResult:
    """Execute all tests for a single scenario.

    *tracked* is the registry the interrupt handler drains.  It is
    passed down into :func:`_start_container`, which registers the
    container from before ``docker run`` so an interrupt during
    start-up cannot leak one, and the entry is removed once the normal
    path has released it — so Ctrl-C part-way through a scenario
    removes the container that is actually running and never tries to
    remove one that has already gone.
    """
    result = ScenarioResult(scenario=scenario)
    log_scenario_banner(scenario, options)

    ctx: _ContainerContext | None = None
    try:
        # Start container.  The registry is handed down so the
        # container is covered from before ``docker run`` rather than
        # only once its ID comes back.
        ctx = _start_container(
            docker,
            scenario,
            options.image,
            index,
            options.creds,
            fetch_every=options.fetch_every,
            tracked=tracked,
        )
        result.container_started = True

        # Wait for Gerrit to be ready
        result.error = await_gerrit_ready(docker, ctx.cid)
        if result.error:
            return result
        result.gerrit_ready = True

        # Give pull-replication time for the first fetch cycle
        wait_initial_cycle(options.fetch_every)

        run_scenario_checks(docker, ctx.cid, scenario, options, result.tests)

    except Exception as exc:
        result.error = f"Unexpected error: {exc}"
        logger.exception("  %s", result.error)

    finally:
        # An interrupt handler may already have removed this container
        # and dropped it from the registry while unwinding through
        # here.  Releasing it again would either repeat the teardown
        # or, under --keep, announce that a container it just removed
        # is still running.  Ownership follows the registry: release
        # only what is still registered.
        owned = tracked is None or ctx is None or ctx in tracked
        if owned:
            release_container(docker, ctx, keep=options.keep)
        # Deregister whichever way release_container went: the
        # container has either been removed, or deliberately kept by
        # --keep, and in both cases a later interrupt must leave it
        # alone.
        if tracked is not None and ctx is not None and ctx in tracked:
            tracked.remove(ctx)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the harness and return the process exit code."""
    args = parse_args()

    debug = os.environ.get("DEBUG", "false").lower() == "true"
    setup_logging(debug=debug)

    # --- List mode ---
    if args.list:
        list_scenarios()
        return 0

    # --- Resolve configuration ---
    config = resolve_harness_config()
    log_harness_config(config, debug=debug)

    # --- Tunnel-only mode ---
    tunnel_results = run_tunnel_tests()
    for t in tunnel_results:
        logger.info(str(t))

    if args.tunnel_only:
        failed = sum(1 for t in tunnel_results if not t.passed)
        return 1 if failed else 0

    # --- Resolve scenarios ---
    selected = select_scenarios(args.scenario)
    if selected is None:
        return 1

    logger.info("Scenarios to run: %s", ", ".join(s.name for s in selected))
    logger.info("")

    # --- Docker setup ---
    docker = DockerManager()
    image = resolve_image(docker, config.gerrit_version, skip_build=args.skip_build)
    if image is None:
        return 1

    # --- Run scenarios ---
    scenario_results: list[ScenarioResult] = []

    # Install signal handler for clean shutdown.  The returned list is
    # the registry the handler drains; each scenario registers its
    # container into it while that container is running.
    shutdown_contexts = install_cleanup_handler(docker)

    for idx, scenario in enumerate(selected):
        # Check credentials before starting container
        try:
            creds = _get_credentials(scenario.gerrit_host)
        except SystemExit:
            sr = ScenarioResult(scenario=scenario)
            sr.error = f"No credentials for {scenario.gerrit_host}"
            scenario_results.append(sr)
            continue

        scenario_results.append(
            run_scenario(
                docker,
                scenario,
                idx,
                ScenarioRunOptions(
                    image=image,
                    creds=creds,
                    timeout=config.timeout,
                    stability_window=config.stability_window,
                    fetch_every=config.fetch_every,
                    keep=args.keep,
                ),
                shutdown_contexts,
            )
        )

    # --- Summary ---
    return print_summary(scenario_results, tunnel_results)


if __name__ == "__main__":
    sys.exit(main())
