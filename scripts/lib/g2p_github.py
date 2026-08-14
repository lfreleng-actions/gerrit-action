# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""GitHub API checks for g2p configuration validation.

Validates that the target GitHub organisation is correctly configured
for ``gerrit_to_platform`` workflow dispatch by checking token
validity, org access, the ``.github`` magic repo, and workflow
naming conventions.

This module is the public entry point for the g2p GitHub tooling.  The
individual checks live in focused sibling modules and are re-exported
here, so callers continue to work with ``g2p_github`` alone:

* :mod:`g2p_github_model` — result record and the ``GERRIT_*`` contract
* :mod:`g2p_github_transport` — transport seam used by the checks
* :mod:`g2p_github_access` — token, org and repository reachability
* :mod:`g2p_github_workflows` — workflow discovery and input validation
* :mod:`g2p_github_orgconfig` — org secret/variable auditing
* :mod:`g2p_github_provision` — org secret/variable writes
* :mod:`g2p_github_audit` — the ordered audit pipeline
* :mod:`g2p_github_report` — annotation, JSON and Markdown rendering

Two things stay here deliberately.  :func:`_github_request` is the
single :mod:`urllib.request` call site shared by every check — all
HTTP goes through it to avoid adding dependencies beyond the standard
library (the ``requests`` package lives in the scripts venv, not the
g2p tools venv).  :func:`provision_org_config` sits alongside the
provisioning primitives it drives so that the two resolve as
attributes of this module.

Usage::

    from g2p_config import G2PConfig
    from g2p_github import check_github_config

    config = G2PConfig.from_environment()
    results = check_github_config(config)
    for r in results:
        print(f"[{r.severity}] {r.check_name}: {r.message}")
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from g2p_github_access import (
    check_magic_repo,
    check_org_access,
    check_repos_exist,
    check_token_valid,
)
from g2p_github_audit import check_github_config
from g2p_github_model import (
    GITHUB_API_BASE,
    GITHUB_GRAPHQL_URL,
    OPTIONAL_ORG_SECRETS,
    REQUIRED_ORG_SECRETS,
    REQUIRED_ORG_VARIABLES,
    REQUIRED_WORKFLOW_INPUTS,
    G2PCheckResult,
)
from g2p_github_orgconfig import check_org_secrets, check_org_variables
from g2p_github_provision import provision_org_secret, provision_org_variable
from g2p_github_report import (
    format_check_results,
    format_check_results_summary,
    results_to_json,
)
from g2p_github_transport import graphql_query as _graphql_query
from g2p_github_workflows import (
    _filter_workflows,
    check_workflow_inputs,
    check_workflows,
)

if TYPE_CHECKING:
    from g2p_config import G2PConfig

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30
"""Default timeout in seconds for HTTP calls."""

__all__ = [
    "GITHUB_API_BASE",
    "GITHUB_GRAPHQL_URL",
    "OPTIONAL_ORG_SECRETS",
    "REQUIRED_ORG_SECRETS",
    "REQUIRED_ORG_VARIABLES",
    "REQUIRED_WORKFLOW_INPUTS",
    "G2PCheckResult",
    # The underscore-prefixed entries are long-standing internals that
    # callers (notably the test suite) import from this module by name;
    # they are listed so the re-export stays explicit.
    "_filter_workflows",
    "_github_request",
    "_graphql_query",
    "check_github_config",
    "check_magic_repo",
    "check_org_access",
    "check_org_secrets",
    "check_org_variables",
    "check_repos_exist",
    "check_token_valid",
    "check_workflow_inputs",
    "check_workflows",
    "format_check_results",
    "format_check_results_summary",
    "provision_org_config",
    "provision_org_secret",
    "provision_org_variable",
    "results_to_json",
]


# ---------------------------------------------------------------------------
# Low-level HTTP helper
# ---------------------------------------------------------------------------


def _github_request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    accept: str = "application/vnd.github+json",
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    """Make an authenticated GitHub API request.

    Parameters
    ----------
    url:
        Full URL to call.
    token:
        GitHub PAT for the ``Authorization`` header.
    method:
        HTTP method.
    body:
        Optional request body (for POST/GraphQL).
    accept:
        ``Accept`` header value.

    Returns
    -------
    tuple[int, dict | list | str]
        HTTP status code and the parsed JSON response (or raw text on
        parse failure).  ``HTTPError`` responses are caught and
        returned as ``(status, body)``; other network-level failures
        propagate as exceptions.

    Raises
    ------
    URLError
        On network-level failures (DNS resolution, connection refused,
        timeout, etc.).  Callers must handle this — each check
        function catches ``URLError`` and returns an appropriate
        :class:`G2PCheckResult`.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            try:
                data: dict[str, Any] | list[Any] | str = json.loads(raw)
            except json.JSONDecodeError:
                data = raw
            return resp.status, data
    except HTTPError as exc:
        raw_err = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw_err)
        except json.JSONDecodeError:
            data = raw_err
        return exc.code, data


# ---------------------------------------------------------------------------
# Provisioning orchestration
# ---------------------------------------------------------------------------


def provision_org_config(
    config: G2PConfig,
    audit_results: list[G2PCheckResult],
    gerrit_info: dict[str, str],
    org_token: str | None = None,
) -> list[G2PCheckResult]:
    """Auto-provision absent org configuration.

    Inspects audit results to determine what is missing, then
    creates secrets and variables as needed.

    Parameters
    ----------
    config:
        G2P configuration.
    audit_results:
        Results from the audit phase.
    gerrit_info:
        Dict with keys: ``ssh_private_key``, ``ssh_host``,
        ``ssh_port``, ``ssh_user``, ``http_url``,
        ``known_hosts``.
    org_token:
        Elevated-permission token for org write ops.
        Falls back to ``config.github_token``.

    Returns
    -------
    list[G2PCheckResult]
        Results of provisioning operations.
    """
    token = org_token or config.github_token
    owner = config.github_owner
    results: list[G2PCheckResult] = []

    # Look up the variables audit entry so we can pick the correct
    # HTTP verb (POST vs PATCH) per variable.  Secrets do not need
    # this lookup because the GitHub Actions secrets API uses a
    # single PUT verb for both create and update.
    variables_check = next(
        (r for r in audit_results if r.check_name == "org_variables"),
        None,
    )

    # Provision required secrets.
    #
    # In ``provision`` mode we ALWAYS overwrite required secrets with
    # the current run's values, regardless of whether they were
    # already present.  Each Gerrit container build produces a fresh
    # ephemeral SSH key, so a "GERRIT_SSH_PRIVKEY exists already"
    # state from a previous run would silently leave the GitHub org
    # holding a stale key that does not match the live Gerrit
    # instance — workflows would dispatch successfully but fail at
    # push time.  Always overwriting keeps Gerrit and the org in
    # lock-step for every provision run.
    ssh_private_key = gerrit_info.get("ssh_private_key", "")
    if ssh_private_key:
        for secret_name in REQUIRED_ORG_SECRETS:
            if secret_name == "GERRIT_SSH_PRIVKEY":
                results.append(
                    provision_org_secret(token, owner, secret_name, ssh_private_key)
                )
    else:
        logger.warning(
            "No SSH private key available; skipping required secret provisioning"
        )
        # Surface this as an explicit failed result so the
        # post-provision audit can flag it.
        results.append(
            G2PCheckResult(
                check_name="provision_secret_GERRIT_SSH_PRIVKEY",
                passed=False,
                message=(
                    "No SSH private key available to provision GERRIT_SSH_PRIVKEY"
                ),
                severity="error",
            )
        )

    # Provision required variables.
    #
    # As with secrets, every required variable is overwritten on each
    # provision run.  Tunnel host/port assignments and known_hosts
    # values can change between runs, and stale variables would
    # cause downstream workflows to talk to the wrong endpoint.
    ssh_host = gerrit_info.get("ssh_host")
    ssh_port = gerrit_info.get("ssh_port")
    gerrit_server = f"{ssh_host}:{ssh_port}" if ssh_host and ssh_port else ""

    variable_map: dict[str, str] = {
        "GERRIT_SERVER": gerrit_server,
        "GERRIT_SSH_USER": gerrit_info.get("ssh_user", ""),
        "GERRIT_KNOWN_HOSTS": gerrit_info.get("known_hosts", ""),
        "GERRIT_URL": gerrit_info.get("http_url", ""),
    }

    # Determine which variables already exist so we can pick the
    # correct HTTP verb (POST for create, PATCH for update).  Use
    # the audit details when available; fall back to assuming the
    # variable does not exist.
    existing_vars: set[str] = set()
    if variables_check is not None:
        details = variables_check.details
        # ``found`` lists variables that exist (any value), while
        # ``empty`` lists those that exist but have an empty value.
        existing_vars.update(details.get("found", []))
        existing_vars.update(details.get("empty", []))

    for var_name in REQUIRED_ORG_VARIABLES:
        value = variable_map.get(var_name, "")
        if not value:
            logger.warning(
                "No value available for variable '%s'; skipping",
                var_name,
            )
            results.append(
                G2PCheckResult(
                    check_name=f"provision_variable_{var_name}",
                    passed=False,
                    message=(f"No value available to provision variable '{var_name}'"),
                    severity="error",
                )
            )
            continue
        results.append(
            provision_org_variable(
                token,
                owner,
                var_name,
                value,
                exists=var_name in existing_vars,
            )
        )

    return results
