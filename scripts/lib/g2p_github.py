# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""GitHub API checks for g2p configuration validation.

Validates that the target GitHub organisation is correctly configured
for ``gerrit_to_platform`` workflow dispatch by checking token
validity, org access, the ``.github`` magic repo, and workflow
naming conventions.

All HTTP calls use :mod:`urllib.request` to avoid adding dependencies
beyond the standard library (the ``requests`` package lives in the
scripts venv, not the g2p tools venv).

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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from g2p_config import G2PConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com"
"""Base URL for the GitHub REST API."""

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
"""URL for the GitHub GraphQL API."""

REQUIRED_WORKFLOW_INPUTS: tuple[str, ...] = (
    "GERRIT_BRANCH",
    "GERRIT_CHANGE_ID",
    "GERRIT_CHANGE_NUMBER",
    "GERRIT_CHANGE_URL",
    "GERRIT_EVENT_TYPE",
    "GERRIT_PATCHSET_NUMBER",
    "GERRIT_PATCHSET_REVISION",
    "GERRIT_PROJECT",
    "GERRIT_REFSPEC",
)
"""Standard ``GERRIT_*`` inputs every g2p workflow must accept."""

_HTTP_TIMEOUT = 30
"""Default timeout in seconds for HTTP calls."""


# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------


@dataclass
class G2PCheckResult:
    """Outcome of a single GitHub-side validation check.

    Attributes:
        check_name: Machine-readable name (e.g. ``"token_valid"``).
        passed: Whether the check succeeded.
        message: Human-readable description of the outcome.
        severity: One of ``"error"``, ``"warning"``, or ``"info"``.
        details: Optional extra data for debugging.
    """

    check_name: str
    passed: bool
    message: str
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        status = "✅" if self.passed else "❌"
        return f"{status} [{self.severity}] {self.check_name}: {self.message}"


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
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


def _graphql_query(
    token: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute a GitHub GraphQL query.

    Parameters
    ----------
    token:
        GitHub PAT.
    query:
        GraphQL query string.
    variables:
        Optional query variables.

    Returns
    -------
    tuple[int, dict]
        HTTP status and the full JSON response body.
    """
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    body = json.dumps(payload).encode("utf-8")
    status, data = _github_request(
        GITHUB_GRAPHQL_URL,
        token,
        method="POST",
        body=body,
    )
    if isinstance(data, dict):
        return status, data
    return status, {"raw": data}


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_token_valid(token: str) -> G2PCheckResult:
    """Verify the token is valid by calling ``GET /user``.

    Parameters
    ----------
    token:
        GitHub PAT to validate.

    Returns
    -------
    G2PCheckResult
        Passed if ``GET /user`` returns 200.
    """
    try:
        status, data = _github_request(f"{GITHUB_API_BASE}/user", token)
    except URLError as exc:
        return G2PCheckResult(
            check_name="token_valid",
            passed=False,
            message=f"Network error checking token: {exc}",
            severity="error",
        )

    if status == 200:
        login = data.get("login", "unknown") if isinstance(data, dict) else "unknown"
        return G2PCheckResult(
            check_name="token_valid",
            passed=True,
            message=f"Token valid (authenticated as {login})",
            severity="info",
            details={"login": login},
        )

    return G2PCheckResult(
        check_name="token_valid",
        passed=False,
        message=f"Token authentication failed (HTTP {status})",
        severity="error",
        details={"status": status},
    )


def check_org_access(token: str, owner: str) -> G2PCheckResult:
    """Verify the token can access the target organisation.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub organisation or user login.

    Returns
    -------
    G2PCheckResult
        Passed if ``GET /orgs/{owner}`` returns 200.
    """
    try:
        status, data = _github_request(f"{GITHUB_API_BASE}/orgs/{owner}", token)
    except URLError as exc:
        return G2PCheckResult(
            check_name="org_access",
            passed=False,
            message=f"Network error checking org {owner}: {exc}",
            severity="error",
        )

    if status == 200:
        return G2PCheckResult(
            check_name="org_access",
            passed=True,
            message=f"Organisation '{owner}' is accessible",
            severity="info",
        )

    if status == 404:
        # Could be a user account instead of an org — try /users
        user_status = 0
        user_error = ""
        try:
            user_status, _ = _github_request(f"{GITHUB_API_BASE}/users/{owner}", token)
        except URLError as user_exc:
            user_status = 0
            user_error = str(user_exc)

        if user_status == 200:
            return G2PCheckResult(
                check_name="org_access",
                passed=True,
                message=f"'{owner}' is a user account (not an org)",
                severity="info",
                details={"account_type": "user"},
            )

        # Build a message that includes the user-check outcome
        msg = f"Organisation '{owner}' not found (HTTP 404)"
        if user_error:
            msg += f"; user check also failed: {user_error}"
        elif user_status != 0:
            msg += f"; user check returned HTTP {user_status}"

        return G2PCheckResult(
            check_name="org_access",
            passed=False,
            message=msg,
            severity="error",
            details={
                "org_status": 404,
                "user_status": user_status,
            },
        )

    return G2PCheckResult(
        check_name="org_access",
        passed=False,
        message=f"Org access check failed for '{owner}' (HTTP {status})",
        severity="error",
        details={"status": status},
    )


def check_magic_repo(token: str, owner: str) -> G2PCheckResult:
    """Verify the ``.github`` magic repository exists.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub organisation or user login.

    Returns
    -------
    G2PCheckResult
        Passed if ``GET /repos/{owner}/.github`` returns 200.
    """
    try:
        status, _ = _github_request(f"{GITHUB_API_BASE}/repos/{owner}/.github", token)
    except URLError as exc:
        return G2PCheckResult(
            check_name="magic_repo",
            passed=False,
            message=f"Network error checking .github repo: {exc}",
            severity="warning",
        )

    if status == 200:
        return G2PCheckResult(
            check_name="magic_repo",
            passed=True,
            message=f"Repository '{owner}/.github' exists",
            severity="info",
        )

    if status == 404:
        return G2PCheckResult(
            check_name="magic_repo",
            passed=False,
            message=(
                f"Repository '{owner}/.github' not found"
                " — required workflows will not work"
            ),
            severity="warning",
        )

    if status in (401, 403):
        return G2PCheckResult(
            check_name="magic_repo",
            passed=False,
            message=(
                f"Unable to access repository '{owner}/.github' "
                f"(HTTP {status} — authentication or permission issue). "
                "Required workflows will be inaccessible."
            ),
            severity="error",
        )

    return G2PCheckResult(
        check_name="magic_repo",
        passed=False,
        message=(
            f"Failed to check repository '{owner}/.github' "
            f"(HTTP {status}). Required workflows may not work."
        ),
        severity="warning",
    )


def check_workflows(
    token: str,
    owner: str,
    repo: str,
    search_filter: str,
) -> G2PCheckResult:
    """Check that a repository has matching Gerrit workflows.

    A workflow matches if its path (filename) contains both ``gerrit``
    and the *search_filter* (e.g. ``verify`` or ``merge``),
    case-insensitively.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub org or user.
    repo:
        Repository name (e.g. ``.github`` or ``ci-management``).
    search_filter:
        Workflow type filter (``"verify"`` or ``"merge"``).

    Returns
    -------
    G2PCheckResult
        Passed if at least one matching active workflow is found.
    """
    check_name = f"workflows_{repo}_{search_filter}"
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/actions/workflows?per_page=100"

    try:
        status, data = _github_request(url, token)
    except URLError as exc:
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=f"Network error listing workflows: {exc}",
            severity="warning",
        )

    if status != 200:
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=(f"Could not list workflows for {owner}/{repo} (HTTP {status})"),
            severity="warning",
            details={"status": status},
        )

    if not isinstance(data, dict):
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message="Unexpected response format from workflows API",
            severity="warning",
        )

    workflows = data.get("workflows", [])
    matching = _filter_workflows(workflows, search_filter)

    if matching:
        names = [w.get("path", w.get("name", "?")) for w in matching]
        return G2PCheckResult(
            check_name=check_name,
            passed=True,
            message=(
                f"Found {len(matching)} '{search_filter}' workflow(s) "
                f"in {owner}/{repo}: {names}"
            ),
            severity="info",
            details={"workflows": names},
        )

    return G2PCheckResult(
        check_name=check_name,
        passed=False,
        message=(
            f"No '{search_filter}' Gerrit workflows found in "
            f"{owner}/{repo} — expected filename containing "
            f"'gerrit' and '{search_filter}'"
        ),
        severity="warning",
        details={"total_workflows": len(workflows)},
    )


def _filter_workflows(
    workflows: list[dict[str, Any]],
    search_filter: str,
) -> list[dict[str, Any]]:
    """Filter workflows by g2p naming convention.

    A workflow matches if:

    - It is ``"active"``
    - Its ``path`` contains ``"gerrit"`` (case-insensitive)
    - Its ``path`` contains *search_filter* (case-insensitive)

    Parameters
    ----------
    workflows:
        List of workflow objects from the GitHub API.
    search_filter:
        The filter keyword (e.g. ``"verify"``).

    Returns
    -------
    list[dict]
        Matching workflow objects.
    """
    results: list[dict[str, Any]] = []
    sf_lower = search_filter.lower()

    for wf in workflows:
        if wf.get("state") != "active":
            continue
        path = wf.get("path", "").lower()
        if "gerrit" in path and sf_lower in path:
            results.append(wf)

    return results


def check_repos_exist(
    token: str,
    owner: str,
    repos: list[str],
) -> G2PCheckResult:
    """Check that specified repositories exist via the REST API.

    Makes individual ``GET /repos/{owner}/{repo}`` calls for each
    repository in the list.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub org or user.
    repos:
        List of repository names to verify.

    Returns
    -------
    G2PCheckResult
        Passed if all repositories were found.
    """
    if not repos:
        return G2PCheckResult(
            check_name="repos_exist",
            passed=True,
            message="No repositories specified for validation",
            severity="info",
        )

    found_names: set[str] = set()
    missing: list[str] = []
    archived: list[str] = []

    for repo in repos:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
        try:
            status, data = _github_request(url, token)
        except URLError as exc:
            return G2PCheckResult(
                check_name="repos_exist",
                passed=False,
                message=f"Network error checking repositories: {exc}",
                severity="warning",
            )

        if status == 404:
            missing.append(repo)
            continue

        if status != 200:
            return G2PCheckResult(
                check_name="repos_exist",
                passed=False,
                message=f"HTTP {status} checking repo '{repo}'",
                severity="warning",
                details={"status": status, "repo": repo},
            )

        if isinstance(data, dict):
            found_names.add(data.get("name", repo))
            if data.get("archived", False):
                archived.append(data.get("name", repo))
        else:
            found_names.add(repo)

    details: dict[str, Any] = {
        "found": sorted(found_names),
        "missing": missing,
        "archived": archived,
    }

    if missing:
        return G2PCheckResult(
            check_name="repos_exist",
            passed=False,
            message=f"Repositories not found: {missing}",
            severity="warning",
            details=details,
        )

    msg = f"All {len(repos)} repositories found in '{owner}'"
    if archived:
        msg += f" (archived: {archived})"

    return G2PCheckResult(
        check_name="repos_exist",
        passed=True,
        message=msg,
        severity="info",
        details=details,
    )


# ---------------------------------------------------------------------------
# Aggregate check runner
# ---------------------------------------------------------------------------


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

    return results


# ---------------------------------------------------------------------------
# Result processing helpers
# ---------------------------------------------------------------------------


def format_check_results(
    results: list[G2PCheckResult],
    mode: str,
) -> tuple[list[str], bool]:
    """Format check results as GitHub Actions annotations.

    Parameters
    ----------
    results:
        Check results from :func:`check_github_config`.
    mode:
        Validation mode (``"error"``, ``"warn"``, or ``"skip"``).

    Returns
    -------
    tuple[list[str], bool]
        A list of annotation strings and a boolean indicating whether
        any fatal failures occurred (only *True* when *mode* is
        ``"error"`` and a check with ``severity="error"`` failed).
    """
    annotations: list[str] = []
    has_fatal = False

    for result in results:
        if result.passed:
            logger.info("%s", result)
            continue

        if result.severity == "error":
            if mode == "error":
                annotations.append(f"::error::{result.message}")
                has_fatal = True
            elif mode == "warn":
                annotations.append(f"::warning::{result.message}")
            # mode == "skip" should never reach here
        elif result.severity == "warning":
            annotations.append(f"::warning::{result.message}")
        else:
            logger.info("%s", result)

    return annotations, has_fatal


def results_to_json(results: list[G2PCheckResult]) -> str:
    """Serialise check results to a JSON string for action outputs.

    Parameters
    ----------
    results:
        Check results.

    Returns
    -------
    str
        JSON array of check result objects.
    """
    return json.dumps(
        [
            {
                "check_name": r.check_name,
                "passed": r.passed,
                "message": r.message,
                "severity": r.severity,
            }
            for r in results
        ],
        indent=2,
    )
