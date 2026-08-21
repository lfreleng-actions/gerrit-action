# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Ordered audit pipeline for a g2p GitHub configuration.

Individual checks live in :mod:`g2p_github_access`,
:mod:`g2p_github_workflows` and :mod:`g2p_github_orgconfig`; this
module owns the *sequence* in which they run and the short-circuit
rules between them.

The checks form a dependency chain — without a token there is nothing
to ask GitHub, and without org access the workflow and repository
probes cannot return meaningful answers — so the pipeline stops at
the first link that breaks rather than emitting a cascade of
consequential failures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from g2p_github_access import (
    check_magic_repo,
    check_org_access,
    check_repos_exist,
    check_token_valid,
)
from g2p_github_model import G2PCheckResult
from g2p_github_workflows import check_workflows

if TYPE_CHECKING:
    from g2p_config import G2PConfig


def check_github_config(
    config: G2PConfig,
) -> list[G2PCheckResult]:
    """Run all applicable GitHub-side validation checks.

    The checks follow a dependency chain: if the token is missing or
    invalid, later checks that need it are skipped.

    Parameters
    ----------
    config:
        A validated :class:`G2PConfig` instance.

    Returns
    -------
    list[G2PCheckResult]
        Ordered list of check outcomes.
    """
    results: list[G2PCheckResult] = []

    # -- Check 1: Token exists -------------------------------------------
    if not config.github_token:
        results.append(
            G2PCheckResult(
                check_name="token_provided",
                passed=False,
                message=(
                    "No GitHub token provided; g2p cannot dispatch "
                    "workflows until a token is configured"
                ),
                severity="warning",
            )
        )
        # Cannot run any API checks without a token.
        return results

    results.append(
        G2PCheckResult(
            check_name="token_provided",
            passed=True,
            message="GitHub token provided",
            severity="info",
        )
    )

    # -- Check 2: Token valid --------------------------------------------
    token_result = check_token_valid(config.github_token)
    results.append(token_result)
    if not token_result.passed:
        # Cannot proceed with an invalid token.
        return results

    # -- Check 3: Org accessible -----------------------------------------
    org_result = check_org_access(config.github_token, config.github_owner)
    results.append(org_result)
    if not org_result.passed:
        return results

    # -- Check 4: .github magic repo -------------------------------------
    results.append(check_magic_repo(config.github_token, config.github_owner))

    # -- Check 5 & 6: Workflow checks ------------------------------------
    if config.validate_workflows:
        # Check .github repo for required workflows
        for search_filter in ("verify", "merge"):
            results.append(
                check_workflows(
                    config.github_token,
                    config.github_owner,
                    ".github",
                    search_filter,
                )
            )

        # Check per-repo workflows
        for repo in config.validate_repos:
            for search_filter in ("verify", "merge"):
                results.append(
                    check_workflows(
                        config.github_token,
                        config.github_owner,
                        repo,
                        search_filter,
                    )
                )

    # -- Check 7: Repositories exist (if specified) ----------------------
    if config.validate_repos:
        results.append(
            check_repos_exist(
                config.github_token,
                config.github_owner,
                config.validate_repos,
            )
        )

    # Note: org-level secret and variable checks intentionally run in
    # a later phase (see ``configure-g2p.py``) so that the audit can
    # re-run *after* provisioning has a chance to create missing items.
    # Running them here would emit warnings for items we are about to
    # create, which is misleading.

    return results
