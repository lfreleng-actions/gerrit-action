# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Argument parsing and run set-up for the replication test harness.

Split out of ``scripts/test-replication-local.py``.  Covers the
command-line interface, the environment-derived tuning record, scenario
selection, image resolution and the interrupt handler, leaving the
entry point with the orchestration only.  Every name here is
re-exported from the harness entry point.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import textwrap
from typing import Any

from config import ConfigError, parse_interval_to_seconds
from docker_manager import DockerManager
from errors import DockerError
from replharness_container import _build_image, _cleanup_container
from replharness_model import (
    _SCENARIO_MAP,
    SCENARIOS,
    HarnessConfig,
    Scenario,
    _ContainerContext,
)

logger = logging.getLogger(__name__)


def _positive_int_env(name: str, default: str) -> int:
    """Read *name* as a positive integer, or exit with a clear message.

    A typo that silently reverted to the default would be worse than a
    failure: the run would proceed under a configuration the developer
    did not ask for and the results would be read as though it had.
    Exiting matches :func:`_get_credentials`, which already ends the
    run when it cannot resolve what it was given.
    """
    raw = os.environ.get(name, default).strip()
    try:
        value = int(raw)
    except ValueError:
        logger.error(
            "%s must be a whole number of seconds; got %r",
            name,
            raw,
        )
        sys.exit(1)

    if value <= 0:
        logger.error("%s must be greater than zero; got %d", name, value)
        sys.exit(1)
    return value


def parse_args() -> argparse.Namespace:
    """Parse the harness command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Local Docker test harness for replication verification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s                          # run all scenarios
              %(prog)s --scenario lf-small      # run a single scenario
              %(prog)s --scenario lf-small,onap # run multiple scenarios
              %(prog)s --list                   # list available scenarios
              %(prog)s --keep                   # keep containers running
              %(prog)s --tunnel-only            # only run tunnel tests
        """),
    )
    parser.add_argument(
        "--scenario",
        "-s",
        help="Comma-separated scenario names to run (default: all).",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available scenarios and exit.",
    )
    parser.add_argument(
        "--keep",
        "-k",
        action="store_true",
        help="Keep containers running after tests (for inspection).",
    )
    parser.add_argument(
        "--tunnel-only",
        action="store_true",
        help="Only run tunnel verification tests (no Docker containers).",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip Docker image build (use stock gerritcodereview/gerrit image).",
    )
    return parser.parse_args()


def list_scenarios() -> None:
    """Print the scenario catalogue for ``--list``."""
    print("\nAvailable test scenarios:\n")
    for s in SCENARIOS:
        flag = " [small-disk]" if s.expect_small_disk else ""
        print(f"  {s.name:20s} {s.gerrit_host}{s.api_path}{flag}")
        print(f"  {'':20s} {s.description}")
        print()


def resolve_harness_config() -> HarnessConfig:
    """Read the harness tuning knobs from the environment.

    Every value that the harness later does arithmetic on is validated
    here, so a bad one is reported by name before any image is built.
    ``FETCH_EVERY`` in particular used to be parsed only inside
    ``wait_initial_cycle``, which runs after a container has already
    been started — so a typo cost a full start-up before surfacing.
    """
    fetch_every = os.environ.get("FETCH_EVERY", "15s").strip()
    try:
        fetch_seconds = parse_interval_to_seconds(fetch_every)
    except ConfigError as exc:
        logger.error("FETCH_EVERY is invalid: %s", exc)
        sys.exit(1)
    if fetch_seconds <= 0:
        logger.error(
            "FETCH_EVERY must be greater than zero; got %r",
            fetch_every,
        )
        sys.exit(1)

    return HarnessConfig(
        gerrit_version=os.environ.get("GERRIT_VERSION", "3.13.1-ubuntu24"),
        plugin_version=os.environ.get("PLUGIN_VERSION", "stable-3.13"),
        timeout=_positive_int_env("REPLICATION_WAIT_TIMEOUT", "180"),
        stability_window=_positive_int_env("STABILITY_WINDOW", "30"),
        fetch_every=fetch_every,
    )


def log_harness_config(config: HarnessConfig, *, debug: bool) -> None:
    """Echo the resolved configuration before any work starts."""
    logger.info("Test configuration:")
    logger.info("  Gerrit version:     %s", config.gerrit_version)
    logger.info("  Plugin version:     %s", config.plugin_version)
    logger.info("  Timeout:            %ds", config.timeout)
    logger.info("  Stability window:   %ds", config.stability_window)
    logger.info("  Fetch every:        %s", config.fetch_every)
    logger.info("  Debug:              %s", debug)
    logger.info("")


def select_scenarios(spec: str | None) -> list[Scenario] | None:
    """Resolve ``--scenario`` into scenarios, or None if a name is unknown."""
    if not spec:
        return list(SCENARIOS)

    names = [n.strip() for n in spec.split(",")]
    selected: list[Scenario] = []
    for name in names:
        if name in _SCENARIO_MAP:
            selected.append(_SCENARIO_MAP[name])
        else:
            logger.error(
                "Unknown scenario: %r  (available: %s)",
                name,
                ", ".join(_SCENARIO_MAP),
            )
            return None
    return selected


def resolve_image(
    docker: DockerManager, gerrit_version: str, *, skip_build: bool
) -> str | None:
    """Pull or build the image the scenarios run on, or None on failure."""
    if skip_build:
        image = f"gerritcodereview/gerrit:{gerrit_version}"
        logger.info("Using stock image: %s", image)
        try:
            docker.run_cmd(["pull", image], timeout=120)
        except DockerError as exc:
            logger.error("Failed to pull image: %s", exc)
            return None
        return image

    try:
        return _build_image(docker, gerrit_version)
    except DockerError as exc:
        logger.error("Failed to build Docker image: %s", exc)
        return None


def install_cleanup_handler(docker: DockerManager) -> list[_ContainerContext]:
    """Install SIGINT/SIGTERM handlers that tear down tracked containers.

    Returns the list the handler drains, so callers can register the
    containers they want removed on interrupt.  A caller that discards
    the return value gets handlers that report an interrupt and exit
    without removing anything.
    """
    shutdown_contexts: list[_ContainerContext] = []

    def _signal_handler(_signum: int, _frame: Any) -> None:
        if shutdown_contexts:
            logger.info(
                "\nInterrupted — removing %d container(s)…",
                len(shutdown_contexts),
            )
            # Drain as we go.  The interrupt unwinds the scenario loop
            # through ``run_scenario``'s finally block, which consults
            # this registry before releasing anything; popping each
            # context as it is cleaned stops that path removing the
            # same container twice, or reporting a container as kept
            # by --keep after this handler has already removed it.
            while shutdown_contexts:
                _cleanup_container(docker, shutdown_contexts.pop())
        else:
            logger.info("\nInterrupted — no containers to clean up")
        sys.exit(130)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    return shutdown_contexts
