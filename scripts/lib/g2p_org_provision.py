# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Org-level audit, provisioning and re-audit phases.

G2P needs a handful of GitHub *organisation* secrets and variables
to exist before the dispatched workflows can talk back to Gerrit.
This module owns that lifecycle: snapshot the initial state,
provision the required items once the containers are up, re-audit
so the reported state is post-provisioning reality, and render the
step summary.

Usage::

    from g2p_org_provision import initial_org_audit, provision_org

    org_results, org_audit_json = initial_org_audit(config)
"""

from __future__ import annotations

import logging
from typing import Any

from config import ActionConfig
from g2p_config import G2PConfig
from g2p_gerrit_info import build_gerrit_info
from g2p_github import (
    G2PCheckResult,
    check_org_secrets,
    check_org_variables,
    format_check_results_summary,
    provision_org_config,
    results_to_json,
)
from g2p_setup import G2PSetupResult
from logging_utils import log_group
from outputs import write_summary

logger = logging.getLogger(__name__)

# Check names that belong in the ``g2p_org_audit_results`` output.
AUDIT_CHECK_NAMES = ("org_secrets", "org_variables", "org_access", "org_audit")


def initial_org_audit(config: G2PConfig) -> tuple[list[G2PCheckResult], str]:
    """Take an initial snapshot of the org-level secrets and variables.

    This phase only *reports* the initial state.  Provisioning (when
    requested) happens in :func:`provision_org` after containers are
    running, and :func:`reaudit_org_state` re-audits so the final
    ``org_audit_json`` reflects the post-provisioning state instead
    of flagging items we were about to create.

    Returns
    -------
    tuple[list[G2PCheckResult], str]
        The audit results and their JSON serialisation.
    """
    org_audit_json = "[]"
    org_results: list[G2PCheckResult] = []

    if config.org_setup == "skip":
        logger.info(
            "Org audit skipped (org_setup=%s)",
            config.org_setup,
        )
        return org_results, org_audit_json

    with log_group("G2P org-level audit (initial)"):
        # Use the same token resolution as provisioning so the
        # audit's read scope matches the elevated token a caller
        # supplied via ``g2p_org_token_map``.  Without this, a
        # least-privileged ``g2p_github_token`` would 403 the
        # /orgs/.../actions/{secrets,variables} reads even when
        # the operator carefully scoped an elevated org token
        # for provisioning — making provision_org_config()
        # blind to which items already exist.
        audit_token = config.resolve_org_token()
        if audit_token:
            org_results.append(
                check_org_secrets(
                    audit_token,
                    config.github_owner,
                )
            )
            org_results.append(
                check_org_variables(
                    audit_token,
                    config.github_owner,
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

        org_audit_json = results_to_json(org_results)
        logger.info(
            "Initial org audit complete (mode=%s)",
            config.org_setup,
        )

    return org_results, org_audit_json


def _record_provision_results(
    prov_results: list[G2PCheckResult],
    provisioned_items: list[str],
) -> bool:
    """Log each provisioning outcome; return True if any one failed."""
    had_fatal = False
    for pr in prov_results:
        if pr.passed:
            provisioned_items.append(pr.message)
            logger.info("Provisioned: %s", pr.message)
        else:
            logger.error(
                "Provisioning failed: %s",
                pr.message,
            )
            had_fatal = True
    return had_fatal


def provision_org(
    config: G2PConfig,
    org_results: list[G2PCheckResult],
    instances: dict[str, dict[str, Any]],
    setup_results: list[G2PSetupResult],
    action_config: ActionConfig,
) -> tuple[list[str], bool]:
    """Provision the required org secrets and variables.

    Always runs when the mode requests it, regardless of the initial
    audit outcome.  Each Gerrit container build produces a fresh
    ephemeral SSH key and may bind to different tunnel host/ports, so
    we cannot rely on the audit reporting items as missing — we must
    overwrite required org secrets and variables on every provision
    run to keep them in sync with the live Gerrit instance.

    ``org_results`` is extended in place with the provisioning
    outcomes.

    Returns
    -------
    tuple[list[str], bool]
        The messages of successfully provisioned items, and whether
        provisioning hit an unrecoverable failure.
    """
    provisioned_items: list[str] = []
    if config.org_setup != "provision":
        return provisioned_items, False

    provision_had_fatal = False
    with log_group("G2P org provisioning"):
        # Build gerrit_info from instances + setup results
        gerrit_info = build_gerrit_info(
            instances,
            setup_results,
            action_config,
        )
        org_token = config.resolve_org_token()

        if not org_token:
            msg = (
                "Cannot provision: no token available "
                "(set g2p_github_token or g2p_org_token_map)"
            )
            logger.error(msg)
            provision_had_fatal = True
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
                config,
                org_results,
                gerrit_info,
                org_token=org_token,
            )
            provision_had_fatal = _record_provision_results(
                prov_results,
                provisioned_items,
            )
            org_results.extend(prov_results)

    return provisioned_items, provision_had_fatal


def reaudit_org_state(
    config: G2PConfig,
    org_results: list[G2PCheckResult],
) -> list[G2PCheckResult]:
    """Replace the initial audit entries with post-provisioning ones.

    The re-audit runs unconditionally whenever ``provision`` mode is
    active (not just when anything was actually provisioned):
    provisioning always overwrites required items, and the initial
    audit may have used the wrong token / hit a permission error,
    leaving stale or missing entries in ``org_results``.  Running the
    audit again with the same elevated token used for provisioning
    guarantees the final output reflects the true post-provision
    state.

    Returns
    -------
    list[G2PCheckResult]
        The refreshed results, or ``org_results`` unchanged when the
        re-audit did not run.
    """
    if config.org_setup != "provision":
        return org_results

    reaudit_token = config.resolve_org_token()
    if not reaudit_token:
        logger.warning(
            "Skipping post-provision re-audit: no GitHub token "
            "available; final g2p_org_audit_results reflects the "
            "pre-provision state.",
        )
        return org_results

    with log_group("G2P org-level audit (post-provision)"):
        fresh_secrets = check_org_secrets(
            reaudit_token,
            config.github_owner,
        )
        fresh_variables = check_org_variables(
            reaudit_token,
            config.github_owner,
        )
        # Keep any non-secret/variable entries (e.g. error
        # breadcrumbs) and replace the original org_secrets /
        # org_variables entries with the refreshed ones.
        preserved = [
            r
            for r in org_results
            if r.check_name not in ("org_secrets", "org_variables")
        ]
        return [fresh_secrets, fresh_variables, *preserved]


def refresh_audit_json(
    config: G2PConfig,
    org_results: list[G2PCheckResult],
    current: str,
) -> str:
    """Recompute the audit JSON after provisioning / re-audit work.

    Keeps only the documented audit checks so the
    ``g2p_org_audit_results`` output stays audit-only.  An allow-list
    (rather than excluding the ``provision_`` prefix) ensures that
    neither provisioning *action* results nor other non-audit
    breadcrumbs such as ``org_provision`` can leak into the output.
    Provisioning outcomes are surfaced separately via
    ``g2p_org_provisioned`` and the step summary.

    Returns
    -------
    str
        The refreshed JSON, or *current* when org setup is skipped.
    """
    if config.org_setup == "skip":
        return current

    audit_only = [r for r in org_results if r.check_name in AUDIT_CHECK_NAMES]
    return results_to_json(audit_only)


def write_g2p_summary(
    config: G2PConfig,
    check_results: list[G2PCheckResult],
    org_results: list[G2PCheckResult],
    provisioned_items: list[str],
) -> None:
    """Render the GitHub step summary, de-duplicating by check name."""
    if not (check_results or org_results):
        return

    seen_names: set[str] = set()
    all_summary_results: list[G2PCheckResult] = []
    for r in check_results + org_results:
        if r.check_name not in seen_names:
            seen_names.add(r.check_name)
            all_summary_results.append(r)
    summary_md = format_check_results_summary(
        results=all_summary_results,
        owner=config.github_owner,
        mode=config.org_setup,
        provisioned=provisioned_items or None,
    )
    write_summary(summary_md)
