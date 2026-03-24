#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Configure gerrit_to_platform inside running Gerrit containers.

This script is the entry point for the G2P configuration step in
``action.yaml``.  It reads ``G2P_*`` environment variables, validates
the configuration, optionally checks the target GitHub organisation,
and then sets up each running Gerrit container with the files and
symlinks that ``gerrit_to_platform`` needs to dispatch workflows.

Steps:

1. Parse ``G2PConfig`` from environment variables.
2. Validate the configuration (fatal errors abort).
3. Run GitHub-side checks (unless ``validation_mode=skip``).
4. Load running instances from ``instances.json``.
5. For each container: deploy INI, hooks, SSH, replication remote.
6. Write G2P outputs to ``$GITHUB_OUTPUT``.

Usage::

    # From action.yaml (via the venv created in the Dockerfile)
    python scripts/configure-g2p.py

    # Locally with environment variables
    G2P_ENABLE=true G2P_GITHUB_OWNER=onap python scripts/configure-g2p.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup – ensure ``scripts/lib`` is importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from config import ActionConfig, InstanceStore  # noqa: E402
from docker_manager import DockerManager  # noqa: E402
from errors import (  # noqa: E402
    G2PCheckError,
    G2PConfigError,
    G2PSetupError,
    GerritActionError,
)
from g2p_config import G2PConfig  # noqa: E402
from g2p_github import (  # noqa: E402
    check_github_config,
    format_check_results,
    results_to_json,
)
from g2p_setup import G2PSetupResult, setup_g2p  # noqa: E402
from logging_utils import log_group, setup_logging  # noqa: E402
from outputs import write_output  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit_g2p_outputs(
    config: G2PConfig,
    results: list[G2PSetupResult],
    check_json: str,
) -> None:
    """Write G2P outputs to ``$GITHUB_OUTPUT``.

    Parameters
    ----------
    config:
        The validated G2P configuration.
    results:
        Setup results from each container.
    check_json:
        JSON string of GitHub check results.
    """
    write_output("g2p_enabled", "true")
    write_output("g2p_github_owner", config.github_owner)
    write_output("g2p_remote_name_style", config.remote_name_style)
    write_output("g2p_token_provided", str(config.token_provided).lower())
    write_output("g2p_validation_results", check_json)

    # Aggregate hooks from all containers
    all_hooks: list[str] = []
    for r in results:
        for h in r.hooks_enabled:
            if h not in all_hooks:
                all_hooks.append(h)
    write_output("g2p_hooks_enabled", json.dumps(all_hooks))

    # Use the first container's config path (they're all the same)
    if results:
        write_output("g2p_config_path", results[0].config_path)

    # Use the first container's SSH public key
    for r in results:
        if r.ssh_public_key:
            write_output("g2p_ssh_public_key", r.ssh_public_key)
            break


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> int:
    """Configure G2P for all running Gerrit instances.

    Returns
    -------
    int
        Exit code: 0 on success, 1 on anticipated error, 2 on
        unexpected error.
    """
    # -- Step 1: Parse config --------------------------------------------
    g2p_config = G2PConfig.from_environment()

    if not g2p_config.enabled:
        logger.info("G2P integration is disabled (g2p_enable=false)")
        write_output("g2p_enabled", "false")
        return 0

    logger.info("G2P integration enabled for '%s'", g2p_config.github_owner)

    # -- Step 2: Validate config -----------------------------------------
    with log_group("G2P configuration validation"):
        errors = g2p_config.check()
        if errors:
            for err in errors:
                logger.error("G2P config error: %s", err)
                print(f"::error::G2P config: {err}", file=sys.stderr)
            raise G2PConfigError(f"G2P configuration has {len(errors)} error(s)")
        logger.info("G2P configuration valid ✅")

    # -- Step 3: GitHub checks -------------------------------------------
    check_json = "[]"
    if g2p_config.validation_mode != "skip":
        with log_group("G2P GitHub checks"):
            check_results = check_github_config(g2p_config)
            check_json = results_to_json(check_results)

            annotations, has_fatal = format_check_results(
                check_results, g2p_config.validation_mode
            )
            for annotation in annotations:
                print(annotation, file=sys.stderr)

            if has_fatal:
                raise G2PCheckError(
                    "GitHub-side checks failed in strict mode",
                    failed_checks=[
                        r.check_name
                        for r in check_results
                        if not r.passed and r.severity == "error"
                    ],
                )

            passed = sum(1 for r in check_results if r.passed)
            total = len(check_results)
            logger.info("GitHub checks: %d/%d passed ✅", passed, total)
    else:
        logger.info("GitHub checks skipped (validation_mode=skip)")

    # -- Step 4: Load running instances ----------------------------------
    action_config = ActionConfig.from_environment()
    setup_logging(debug=action_config.debug)

    instance_store = InstanceStore(action_config.instances_json_path)
    instances = instance_store.load()

    if not instances:
        logger.warning(
            "No running instances found in %s — "
            "G2P config will be generated but not deployed",
            action_config.instances_json_path,
        )
        _emit_g2p_outputs(g2p_config, [], check_json)
        return 0

    # -- Step 5: Configure each container --------------------------------
    docker = DockerManager()
    setup_results: list[G2PSetupResult] = []

    for slug, meta in instances.items():
        cid = meta.get("cid", "")
        if not cid:
            logger.warning("Instance '%s' has no container ID — skipping", slug)
            continue

        with log_group(f"G2P setup: {slug} ({cid[:12]})"):
            result = setup_g2p(g2p_config, docker, cid)
            setup_results.append(result)

            logger.info(
                "Instance '%s': config=%s, hooks=%s",
                slug,
                result.config_path,
                result.hooks_enabled,
            )

    # -- Step 6: Emit outputs --------------------------------------------
    with log_group("G2P outputs"):
        _emit_g2p_outputs(g2p_config, setup_results, check_json)

        logger.info(
            "G2P configured %d instance(s) ✅",
            len(setup_results),
        )

    return 0


def main() -> int:
    """Entry point with structured error handling."""
    setup_logging()
    try:
        return run()
    except G2PConfigError as exc:
        logger.error("G2P configuration error: %s", exc)
        return 1
    except G2PCheckError as exc:
        logger.error(
            "G2P GitHub check failure: %s (checks: %s)",
            exc,
            exc.failed_checks,
        )
        return 1
    except G2PSetupError as exc:
        logger.error("G2P setup error: %s", exc)
        return 1
    except GerritActionError as exc:
        logger.error("Gerrit action error: %s", exc)
        return 1
    except Exception as exc:
        logger.exception("Unexpected error during G2P configuration: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
