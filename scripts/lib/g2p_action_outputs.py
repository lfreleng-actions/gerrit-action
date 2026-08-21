# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Everything the G2P step reports back to the caller.

Two audiences are served here: the workflow (via ``$GITHUB_OUTPUT``
key/value pairs) and the human reading the console (via a single
``✅``/``❌`` line and the process exit code).  Grouping them means
the contract between this action and its consumers lives in one
place.

Usage::

    from g2p_action_outputs import emit_g2p_outputs, final_exit_code

    emit_g2p_outputs(config, results, check_json)
    return final_exit_code(config, org_results, ...)
"""

from __future__ import annotations

import json
import logging

from g2p_config import G2PConfig
from g2p_github import G2PCheckResult
from g2p_setup import G2PSetupResult
from outputs import write_output

logger = logging.getLogger(__name__)


def emit_g2p_outputs(
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


def emit_final_status(success: bool, reason: str = "") -> None:
    """Print a single ✅/❌ summary line to stdout.

    Bypasses the logger deliberately so this final status always
    appears as plain stdout regardless of earlier warnings, and is
    easy for a human to spot in the workflow console.
    """
    if success:
        print("✅ Gerrit2Platform configuration succeeded")
    else:
        suffix = f": {reason}" if reason else ""
        print(f"❌ Gerrit2Platform configuration failed{suffix}")


def final_exit_code(
    config: G2PConfig,
    org_results: list[G2PCheckResult],
    *,
    provision_had_fatal: bool,
    selftest_had_errors: bool,
) -> int:
    """Emit the final status line and decide the process exit code.

    Parameters
    ----------
    config:
        The validated G2P configuration.
    org_results:
        Org-level audit results after any provisioning / re-audit.
    provision_had_fatal:
        Whether org provisioning reported an unrecoverable failure.
    selftest_had_errors:
        Whether any container's plumbing self-test reported an
        error-severity check failure.

    Returns
    -------
    int
        ``0`` on success, ``1`` when the step must go red.
    """
    if provision_had_fatal:
        emit_final_status(False, "org provisioning failed")
        return 1

    # A failed plumbing self-test means hooks won't fire or the
    # script can't run — dispatch will silently never happen.
    # Surface this as a failure so the deploy step itself goes red,
    # rather than letting the user discover it later when no CI
    # runs appear in the target org.
    if selftest_had_errors:
        emit_final_status(
            False,
            "G2P plumbing self-test reported error(s); see preceding logs",
        )
        return 1

    # In provision mode, any remaining absent required secrets or
    # variables mean the caller's downstream workflows will still be
    # broken — surface that as a failure.
    if config.org_setup == "provision":
        # Only error-severity results indicate required items are
        # still absent.  Warning-severity results (e.g. optional
        # recommended secrets missing) are informational and must
        # not fail the step.
        post_failures = [
            r
            for r in org_results
            if r.check_name in ("org_secrets", "org_variables")
            and not r.passed
            and r.severity == "error"
        ]
        if post_failures:
            names = ", ".join(r.check_name for r in post_failures)
            emit_final_status(
                False,
                f"required org config still absent ({names})",
            )
            return 1

        # Surface any non-fatal warnings for visibility without
        # failing the overall step.
        post_warnings = [
            r
            for r in org_results
            if r.check_name in ("org_secrets", "org_variables")
            and not r.passed
            and r.severity == "warning"
        ]
        for r in post_warnings:
            logger.warning(
                "Post-provision audit advisory (%s): %s",
                r.check_name,
                r.message,
            )

    emit_final_status(True)
    return 0
