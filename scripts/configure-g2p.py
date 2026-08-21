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

The individual phases live in ``scripts/lib``: :mod:`g2p_deploy`
(config validation, GitHub checks, container deployment),
:mod:`g2p_org_provision` (org-level audit and provisioning) and
:mod:`g2p_action_outputs` (outputs, final status, exit code).  This
file only sequences them.

Usage::

    # From action.yaml (via the venv created in the Dockerfile)
    python scripts/configure-g2p.py

    # Locally with environment variables
    G2P_ENABLE=true G2P_GITHUB_OWNER=onap python scripts/configure-g2p.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup – ensure ``scripts/lib`` is importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from config import ActionConfig  # noqa: E402
from docker_manager import DockerManager  # noqa: E402
from errors import (  # noqa: E402
    G2PCheckError,
    G2PConfigError,
    G2PSetupError,
    GerritActionError,
)
from g2p_action_outputs import (  # noqa: E402
    emit_final_status,
    emit_g2p_outputs,
    final_exit_code,
)
from g2p_config import G2PConfig  # noqa: E402
from g2p_deploy import (  # noqa: E402
    configure_instances,
    load_running_instances,
    run_github_checks,
    validate_g2p_config,
)
from g2p_org_provision import (  # noqa: E402
    initial_org_audit,
    provision_org,
    reaudit_org_state,
    refresh_audit_json,
    write_g2p_summary,
)
from logging_utils import log_group, setup_logging  # noqa: E402
from outputs import write_output  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _pynacl_available() -> bool:
    """Return True when PyNaCl can be imported."""
    try:
        import nacl.public  # noqa: F401  # pyright: ignore[reportMissingImports]
    except ImportError:
        return False
    return True


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

    # -- Step 1b: PyNaCl precheck (provision mode only) ------------------
    # Provisioning org-level secrets requires PyNaCl for sealed-box
    # encryption.  Check early so we fail fast rather than after all
    # the setup work.  Other modes (verify / skip) never touch PyNaCl
    # so we do not import it or warn about it.
    if g2p_config.org_setup == "provision" and not _pynacl_available():
        logger.error(
            "PyNaCl is required for org provisioning but is not "
            "available; install it on the runner with "
            "'python3 -m pip install --user PyNaCl' before "
            "enabling g2p_org_setup=provision",
        )
        emit_final_status(False, "PyNaCl missing (provision mode)")
        return 1

    # -- Step 2: Validate config -----------------------------------------
    validate_g2p_config(g2p_config)

    # -- Step 3: GitHub checks -------------------------------------------
    check_results, check_json = run_github_checks(g2p_config)

    # -- Step 3b: Org-level audit (initial snapshot) ---------------------
    org_results, org_audit_json = initial_org_audit(g2p_config)
    org_provisioned = False

    # -- Step 4: Load running instances ----------------------------------
    action_config = ActionConfig.from_environment()
    setup_logging(debug=action_config.debug)

    instances = load_running_instances(action_config)

    if not instances:
        logger.warning(
            "No running instances found in %s — "
            "G2P config will be generated but not deployed",
            action_config.instances_json_path,
        )
        emit_g2p_outputs(g2p_config, [], check_json, org_audit_json, org_provisioned)
        return 0

    # -- Step 5: Configure each container --------------------------------
    docker = DockerManager()
    setup_results, selftest_reports = configure_instances(
        g2p_config,
        docker,
        instances,
    )
    selftest_had_errors = any(r.has_errors for r in selftest_reports)

    # -- Step 5b: Org provisioning (after containers are configured) ------
    provisioned_items, provision_had_fatal = provision_org(
        g2p_config,
        org_results,
        instances,
        setup_results,
        action_config,
    )
    org_provisioned = bool(provisioned_items)

    # -- Step 5c: Re-audit org state after provisioning -----------------
    org_results = reaudit_org_state(g2p_config, org_results)
    org_audit_json = refresh_audit_json(g2p_config, org_results, org_audit_json)

    # -- Step 6: Write step summary --------------------------------------
    write_g2p_summary(g2p_config, check_results, org_results, provisioned_items)

    # -- Step 7: Emit outputs --------------------------------------------
    with log_group("G2P outputs"):
        emit_g2p_outputs(
            g2p_config,
            setup_results,
            check_json,
            org_audit_json,
            org_provisioned,
        )

        logger.info(
            "G2P configured %d instance(s)",
            len(setup_results),
        )

    # -- Step 8: Final single-line status -------------------------------
    return final_exit_code(
        g2p_config,
        org_results,
        provision_had_fatal=provision_had_fatal,
        selftest_had_errors=selftest_had_errors,
    )


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
