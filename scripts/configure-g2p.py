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
from typing import Any

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
    G2PCheckResult,
    check_github_config,
    format_check_results,
    format_check_results_summary,
    provision_org_config,
    results_to_json,
)
from g2p_setup import G2PSetupResult, setup_g2p  # noqa: E402
from logging_utils import log_group, setup_logging  # noqa: E402
from outputs import write_output, write_summary  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit_g2p_outputs(
    config: G2PConfig,
    results: list[G2PSetupResult],
    check_json: str,
    org_audit_json: str = "[]",
    org_provisioned: bool = False,
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
    org_audit_json:
        JSON string of org-level audit check results.
    org_provisioned:
        Whether any org-level items were actually provisioned.
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

    write_output("g2p_org_audit_results", org_audit_json)
    write_output("g2p_org_provisioned", str(org_provisioned).lower())


# ---------------------------------------------------------------------------
# Gerrit info builder (for org provisioning)
# ---------------------------------------------------------------------------


def _build_gerrit_info(
    instances: dict[str, dict[str, Any]],
    setup_results: list[G2PSetupResult],
    action_config: ActionConfig,
) -> dict[str, str]:
    """Build the ``gerrit_info`` dict for org provisioning.

    Extracts connection metadata from the first running instance
    and the G2P setup results so that ``provision_org_config`` can
    populate org-level secrets and variables.

    The host and port values are derived from the same tunnel /
    localhost logic used by ``start-instances.py`` so they point
    at the *running container*, not the source Gerrit server.

    Parameters
    ----------
    instances:
        Loaded ``instances.json`` data.
    setup_results:
        Results from :func:`setup_g2p` for each container.
    action_config:
        The global :class:`ActionConfig`.

    Returns
    -------
    dict[str, str]
        Keys: ``ssh_private_key``, ``ssh_host``, ``ssh_port``,
        ``ssh_user``, ``known_hosts``, ``http_url``.
    """
    info: dict[str, str] = {}

    # Use first instance for connection metadata
    if instances:
        first_slug = sorted(instances.keys())[0]
        meta = instances[first_slug]

        # Resolve effective host/ports using the same logic as
        # _resolve_tunnel() in start-instances.py: tunnel host +
        # tunnel ports when configured, otherwise localhost +
        # the container's mapped ports.
        tunnel_host = action_config.tunnel_host
        tunnel_ports = action_config.tunnel_ports
        tc = tunnel_ports.get(first_slug) if tunnel_host else None

        if tunnel_host and tc:
            ssh_host = tunnel_host
            ssh_port = str(tc.ssh_port)
            http_port = str(tc.http_port)
        else:
            ssh_host = "localhost"
            ssh_port = str(meta.get("ssh_port", ""))
            http_port = str(meta.get("http_port", ""))

        info["ssh_host"] = ssh_host
        info["ssh_port"] = ssh_port
        info["ssh_user"] = action_config.ssh_auth_username or "admin"

        # HTTP URL: construct from effective host/port, optionally
        # appending the API path when USE_API_PATH is enabled.
        api_path = meta.get("api_path", "")
        if action_config.use_api_path and api_path:
            # Normalise: ensure leading /, strip trailing /
            if not api_path.startswith("/"):
                api_path = f"/{api_path}"
            api_path = api_path.rstrip("/")
            info["http_url"] = f"http://{ssh_host}:{http_port}{api_path}/"
        else:
            info["http_url"] = f"http://{ssh_host}:{http_port}/"

        # Build known_hosts from captured SSH host keys
        host_keys = meta.get("ssh_host_keys", {})
        kh_lines: list[str] = []
        for _key_type, key_data in sorted(host_keys.items()):
            if key_data and ssh_host:
                kh_lines.append(f"{ssh_host} {key_data}")
        info["known_hosts"] = "\n".join(kh_lines)

    # SSH private key from the first setup result
    for r in setup_results:
        if r.ssh_private_key:
            info["ssh_private_key"] = r.ssh_private_key
            break

    return info


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
    check_results: list[G2PCheckResult] = []
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

    # -- Step 3b: Org-level audit ----------------------------------------
    org_audit_json = "[]"
    org_provisioned = False
    provisioned_items: list[str] = []
    org_results: list[G2PCheckResult] = []

    if g2p_config.org_setup != "skip":
        with log_group("G2P org-level audit"):
            # When validation ran, org checks are in check_results.
            # Otherwise, run them standalone.
            if check_results:
                org_results = [
                    r
                    for r in check_results
                    if r.check_name in ("org_secrets", "org_variables")
                ]
                # If GitHub checks ran but exited early (e.g. token
                # or org_access failure), org-specific results won't
                # be present.  Emit a synthetic result so the output
                # always explains why org checks weren't executed.
                if not org_results:
                    org_results.append(
                        G2PCheckResult(
                            check_name="org_audit",
                            passed=False,
                            message=(
                                "Org audit could not run because "
                                "earlier GitHub checks failed or "
                                "aborted before org-level checks. "
                                "See the g2p_validation_results "
                                "output for details."
                            ),
                            severity="warning",
                        )
                    )
            else:
                # Org audit requested but GitHub checks were
                # skipped; run org checks directly
                from g2p_github import (
                    check_org_secrets,
                    check_org_variables,
                )

                org_results = []
                if g2p_config.github_token:
                    org_results.append(
                        check_org_secrets(
                            g2p_config.github_token,
                            g2p_config.github_owner,
                        )
                    )
                    org_results.append(
                        check_org_variables(
                            g2p_config.github_token,
                            g2p_config.github_owner,
                        )
                    )
                else:
                    msg = "Org audit requires a GitHub token; skipping org checks"
                    logger.warning(msg)
                    org_results.append(
                        G2PCheckResult(
                            check_name="org_audit",
                            passed=False,
                            message=msg,
                            severity="warning",
                        )
                    )

            # Provisioning happens after container setup
            # (Step 5b below) when gerrit_info is available.

            org_audit_json = results_to_json(org_results)

            logger.info("Org audit complete (mode=%s)", g2p_config.org_setup)
    else:
        logger.info("Org audit skipped (org_setup=%s)", g2p_config.org_setup)

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
        _emit_g2p_outputs(g2p_config, [], check_json, org_audit_json, org_provisioned)
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

    # -- Step 5b: Org provisioning (after containers are configured) ------
    if g2p_config.org_setup == "provision" and org_results:
        with log_group("G2P org provisioning"):
            # Build gerrit_info from instances + setup results
            gerrit_info = _build_gerrit_info(
                instances,
                setup_results,
                action_config,
            )
            org_token = g2p_config.resolve_org_token()

            if not org_token:
                msg = (
                    "Cannot provision: no token available "
                    "(set g2p_github_token or g2p_org_token_map)"
                )
                logger.warning(msg)
                org_results.append(
                    G2PCheckResult(
                        check_name="org_provision",
                        passed=False,
                        message=msg,
                        severity="error",
                    )
                )
            else:
                prov_results = provision_org_config(
                    g2p_config,
                    org_results,
                    gerrit_info,
                    org_token=org_token,
                )
                for pr in prov_results:
                    if pr.passed:
                        provisioned_items.append(pr.message)
                        logger.info("Provisioned: %s", pr.message)
                    else:
                        logger.warning(
                            "Provisioning failed: %s",
                            pr.message,
                        )
                org_results.extend(prov_results)
                org_provisioned = bool(provisioned_items)

            # Recompute JSON after provisioning results
            org_audit_json = results_to_json(org_results)

    # -- Step 6: Write step summary --------------------------------------
    if check_results or org_results:
        seen_names: set[str] = set()
        all_summary_results: list[G2PCheckResult] = []
        for r in check_results + org_results:
            if r.check_name not in seen_names:
                seen_names.add(r.check_name)
                all_summary_results.append(r)
        summary_md = format_check_results_summary(
            results=all_summary_results,
            owner=g2p_config.github_owner,
            mode=g2p_config.org_setup,
            provisioned=provisioned_items or None,
        )
        write_summary(summary_md)

    # -- Step 7: Emit outputs --------------------------------------------
    with log_group("G2P outputs"):
        _emit_g2p_outputs(
            g2p_config,
            setup_results,
            check_json,
            org_audit_json,
            org_provisioned,
        )

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
