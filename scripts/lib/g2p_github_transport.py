# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub API transport used by the g2p check modules.

Every GitHub call made by the g2p validation suite funnels through a
single :func:`urllib.request.urlopen` call site, which lives in
:mod:`g2p_github` (:func:`g2p_github._github_request`).  Keeping one
call site means transport concerns — auth headers, timeouts, JSON
decoding, ``HTTPError`` handling — are implemented exactly once.

:func:`github_request` here is the seam the check modules use to reach
that call site.  It resolves :mod:`g2p_github` lazily, at call time,
so the check modules can be loaded in any order.  A module-level
dependency on :mod:`g2p_github` would instead be circular, because
:mod:`g2p_github` re-exports the check functions.

:func:`graphql_query` builds on that seam and owns the GraphQL
request envelope: payload construction and normalising the response
body to a mapping.
"""

from __future__ import annotations

import json
from typing import Any

from g2p_github_model import GITHUB_GRAPHQL_URL


def github_request(
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
        parse failure).

    Raises
    ------
    URLError
        On network-level failures; see
        :func:`g2p_github._github_request`.
    """
    from g2p_github import _github_request

    return _github_request(
        url,
        token,
        method=method,
        body=body,
        accept=accept,
    )


def graphql_query(
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
    status, data = github_request(
        GITHUB_GRAPHQL_URL,
        token,
        method="POST",
        body=body,
    )
    if isinstance(data, dict):
        return status, data
    return status, {"raw": data}
