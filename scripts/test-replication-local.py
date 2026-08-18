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

The moving parts live in ``scripts/lib``: the scenario catalogue in
``harness_scenarios``, run settings and credentials in
``harness_config``, container lifecycle in ``harness_containers``, the
per-container assertions in ``harness_checks``, the container-free
tunnel assertions in ``harness_tunnel_checks``, result types and the
final summary in ``harness_results``, and the run sequence itself in
``harness_runner``.  This file is the CLI front-end.

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

import argparse
import logging
import os
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from docker_manager import DockerManager  # noqa: E402
from errors import DockerError  # noqa: E402
from harness_config import HarnessSettings, log_settings  # noqa: E402
from harness_containers import build_image  # noqa: E402
from harness_results import print_summary  # noqa: E402
from harness_runner import run_all_scenarios  # noqa: E402
from harness_scenarios import print_scenario_listing, select_scenarios  # noqa: E402
from harness_tunnel_checks import run_tunnel_tests  # noqa: E402
from logging_utils import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
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


def _resolve_image(
    docker: DockerManager, settings: HarnessSettings, *, skip_build: bool
) -> str | None:
    """Pull or build the image the scenarios run against.

    Returns the image reference, or ``None`` if Docker could not
    provide one (already reported to the log).
    """
    if skip_build:
        image = f"gerritcodereview/gerrit:{settings.gerrit_version}"
        logger.info("Using stock image: %s", image)
        try:
            docker.run_cmd(["pull", image], timeout=120)
        except DockerError as exc:
            logger.error("Failed to pull image: %s", exc)
            return None
        return image

    try:
        return build_image(
            docker, settings.gerrit_version, dockerfile_dir=SCRIPT_DIR.parent
        )
    except DockerError as exc:
        logger.error("Failed to build Docker image: %s", exc)
        return None


def main() -> int:
    args = parse_args()

    debug = os.environ.get("DEBUG", "false").lower() == "true"
    setup_logging(debug=debug)

    # --- List mode ---
    if args.list:
        print_scenario_listing()
        return 0

    # --- Resolve configuration ---
    settings = HarnessSettings.from_env(debug=debug)
    log_settings(settings)

    # --- Tunnel-only mode ---
    tunnel_results = run_tunnel_tests(SCRIPT_DIR / "verify-tunnel.py")
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

    image = _resolve_image(docker, settings, skip_build=args.skip_build)
    if image is None:
        return 1

    # --- Run scenarios ---
    scenario_results = run_all_scenarios(
        docker,
        selected,
        image=image,
        settings=settings,
        keep=args.keep,
    )

    # --- Summary ---
    return print_summary(scenario_results, tunnel_results)


if __name__ == "__main__":
    sys.exit(main())
