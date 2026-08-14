# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Discovery and validation of Gerrit workflows on GitHub.

Covers the two workflow-shaped questions the g2p audit asks:

* Does a repository publish workflows that follow the g2p naming
  convention (``gerrit`` plus ``verify``/``merge`` in the filename)?
* Does a given workflow file declare the full ``GERRIT_*``
  ``workflow_dispatch`` input contract that ``gerrit_to_platform``
  populates when it dispatches a run?

Workflow listing uses the REST API; reading a workflow file's contents
uses GraphQL, which returns the blob text in a single round trip.
"""

from __future__ import annotations

from typing import Any
from urllib.error import URLError

from g2p_github_model import (
    GITHUB_API_BASE,
    REQUIRED_WORKFLOW_INPUTS,
    G2PCheckResult,
)
from g2p_github_transport import github_request, graphql_query

_WORKFLOW_CONTENT_QUERY = """
    query WorkflowContent($owner: String!, $repo: String!, $expr: String!) {
      repository(owner: $owner, name: $repo) {
        object(expression: $expr) {
          ... on Blob {
            text
          }
        }
      }
    }
    """
"""GraphQL query fetching a single workflow file's blob text."""


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
        status, data = github_request(url, token)
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


def _extract_blob(response: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the ``Blob`` node in a workflow-content GraphQL response.

    The response is walked one level at a time — ``data``, then
    ``repository``, then ``object`` — so that a node which is absent,
    ``null`` (GitHub returns ``repository: null`` for a repo the token
    cannot see) or not a mapping is an explicit, distinguishable
    outcome instead of an empty mapping that reads like a successful
    lookup.

    Parameters
    ----------
    response:
        Full GraphQL JSON response body.

    Returns
    -------
    dict | None
        The blob node when it carries a ``text`` key, otherwise
        ``None`` to signal that the file content is unavailable.
    """
    payload = response.get("data")
    if not isinstance(payload, dict):
        return None

    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None

    blob = repository.get("object")
    if not isinstance(blob, dict) or "text" not in blob:
        return None

    return blob


def check_workflow_inputs(
    token: str,
    owner: str,
    repo: str,
    workflow_path: str,
) -> G2PCheckResult:
    """Verify a workflow file has required GERRIT_* inputs.

    Uses GraphQL to fetch file content, then parses the YAML
    to check for required ``workflow_dispatch`` inputs.

    Parameters
    ----------
    token:
        GitHub PAT.
    owner:
        GitHub org or user.
    repo:
        Repository name.
    workflow_path:
        Path to the workflow file (e.g.
        ``.github/workflows/gerrit-verify.yaml``).

    Returns
    -------
    G2PCheckResult
        Passed if all required inputs are present.
    """
    check_name = f"workflow_inputs_{repo}_{workflow_path.split('/')[-1]}"

    variables = {
        "owner": owner,
        "repo": repo,
        "expr": f"HEAD:{workflow_path}",
    }

    try:
        status, data = graphql_query(token, _WORKFLOW_CONTENT_QUERY, variables)
    except URLError as exc:
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=f"Network error fetching workflow content: {exc}",
            severity="warning",
        )

    if status != 200:
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=(f"Failed to fetch workflow content (HTTP {status})"),
            severity="warning",
            details={"status": status},
        )

    blob = _extract_blob(data)
    if blob is None:
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=(
                f"Could not retrieve content of {workflow_path} in {owner}/{repo}"
            ),
            severity="warning",
        )

    # Parse the YAML content
    try:
        import yaml

        workflow = yaml.safe_load(blob["text"])
    except Exception as exc:
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=(f"Failed to parse {workflow_path}: {exc}"),
            severity="warning",
        )

    if not isinstance(workflow, dict):
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=f"Workflow {workflow_path} is not a valid YAML mapping",
            severity="warning",
        )

    return _evaluate_workflow_inputs(workflow, check_name, owner, repo, workflow_path)


def _evaluate_workflow_inputs(
    workflow: dict[Any, Any],
    check_name: str,
    owner: str,
    repo: str,
    workflow_path: str,
) -> G2PCheckResult:
    """Compare a parsed workflow's dispatch inputs to the contract.

    Parameters
    ----------
    workflow:
        Parsed workflow YAML mapping. Keys are deliberately untyped:
        YAML 1.1 parses a bare ``on:`` key as the boolean ``True``,
        so a workflow mapping is not guaranteed to be string-keyed.
    check_name:
        Name to record on the returned result.
    owner:
        GitHub org or user (for messages).
    repo:
        Repository name (for messages).
    workflow_path:
        Path to the workflow file (for messages).

    Returns
    -------
    G2PCheckResult
        Passed if all required inputs are present.
    """
    # Extract workflow_dispatch inputs. YAML 1.1 resolves a bare ``on:``
    # key to the boolean ``True``, so accept either spelling: quoted
    # ("on") from YAML 1.2 parsers, or True from PyYAML's safe_load.
    on_block = workflow.get("on", workflow.get(True, {}))
    if isinstance(on_block, dict):
        dispatch = on_block.get("workflow_dispatch", {})
    else:
        dispatch = {}

    if not isinstance(dispatch, dict):
        dispatch = {}

    inputs = dispatch.get("inputs", {})
    if not isinstance(inputs, dict):
        inputs = {}

    input_names = set(inputs.keys())
    missing = [name for name in REQUIRED_WORKFLOW_INPUTS if name not in input_names]

    details: dict[str, Any] = {
        "missing": missing,
        "found": [name for name in REQUIRED_WORKFLOW_INPUTS if name in input_names],
        "workflow_path": workflow_path,
    }

    if missing:
        return G2PCheckResult(
            check_name=check_name,
            passed=False,
            message=(
                f"Workflow {workflow_path} in {owner}/{repo} "
                f"is missing required input(s): {missing}"
            ),
            severity="warning",
            details=details,
        )

    return G2PCheckResult(
        check_name=check_name,
        passed=True,
        message=(
            f"Workflow {workflow_path} has all "
            f"{len(REQUIRED_WORKFLOW_INPUTS)} required inputs"
        ),
        severity="info",
        details=details,
    )
