# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Querying the source Gerrit's REST API for its project list.

This module owns the one outbound REST call made while provisioning an
instance: asking the *source* Gerrit which projects exist, so their
bare repositories can be pre-created locally before pull-replication
starts polling.

It deliberately degrades gracefully — every failure path logs a warning
and returns an empty list rather than raising, because a missing
project list must not abort the whole instance startup.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import requests
from config import ActionConfig

logger = logging.getLogger(__name__)

# Gerrit API responses carry this XSSI-protection prefix
_XSSI_PREFIX = ")]}'\n"


def fetch_remote_projects(
    gerrit_host: str,
    api_path: str,
    project_filter: str,
    max_projects: int,
    config: ActionConfig,
) -> list[str]:
    """Fetch the project list from a remote Gerrit server's REST API.

    Parameters
    ----------
    gerrit_host:
        Hostname of the remote Gerrit server.
    api_path:
        Detected API path prefix (e.g. ``"/r"``).
    project_filter:
        Regex or empty string to filter projects.
    max_projects:
        Maximum number of projects to return.
    config:
        Action config (for auth credentials).

    Returns
    -------
    list[str]
        Project names (keys from the Gerrit ``/projects/`` endpoint).
    """
    logger.info("Fetching project list from %s…", gerrit_host)

    # Build URL
    path = api_path.strip("/")
    if path:
        base_url = f"https://{gerrit_host}/{path}/projects/"
    else:
        base_url = f"https://{gerrit_host}/projects/"

    params: dict[str, str] = {"n": str(max_projects)}
    if project_filter and project_filter != ".*":
        params["r"] = project_filter
    # Restrict to ACTIVE projects so READ_ONLY (archived) and HIDDEN
    # projects are excluded server-side.  Gerrit's REST API natively
    # supports the ``state`` query parameter, so we let the source
    # do the filtering rather than fetching everything and dropping
    # archived entries locally.  Operators who want archived repos
    # mirrored too can set ``SKIP_ARCHIVED_PROJECTS=false`` (env) or
    # ``skip_archived_projects: 'false'`` (action input).
    if config.skip_archived_projects:
        params["state"] = "ACTIVE"
        logger.info("  Restricting to ACTIVE projects (SKIP_ARCHIVED_PROJECTS=true)")
    else:
        logger.info(
            "  Including archived (READ_ONLY) projects (SKIP_ARCHIVED_PROJECTS=false)"
        )

    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    full_url = f"{base_url}?{query}"
    logger.info("  API URL: %s", full_url)

    # Build request kwargs
    kwargs: dict[str, Any] = {"timeout": (30, 60)}
    auth_type = config.auth_type.lower()

    if auth_type == "http_basic" and config.http_username and config.http_password:
        kwargs["auth"] = (config.http_username, config.http_password)
        logger.info("  Using HTTP basic authentication")
    elif auth_type == "bearer_token" and config.bearer_token:
        kwargs["headers"] = {"Authorization": f"Bearer {config.bearer_token}"}
        logger.info("  Using bearer token authentication")
    else:
        logger.info("  Using anonymous access for REST API")

    try:
        resp = requests.get(full_url, **kwargs)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch project list from %s: %s", gerrit_host, exc)
        return []

    # Strip XSSI prefix and parse JSON
    body = resp.text
    if body.startswith(_XSSI_PREFIX):
        body = body[len(_XSSI_PREFIX) :]
    elif body.startswith(")]}'"):
        # Variant without trailing newline
        body = body.split("\n", 1)[-1]

    try:
        data = json.loads(body)
        projects = list(data.keys())
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("Failed to parse project list response: %s", exc)
        return []

    logger.info("  Found %d projects on remote server", len(projects))
    return projects
