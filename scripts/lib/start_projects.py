# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Remote project discovery and local pre-creation.

Split out of ``start-instances.py``.  Lists the projects the source
server exposes over REST, and pre-creates the matching bare
repositories in the instance's ``git`` directory — ``fetchEvery`` only
polls repositories Gerrit already knows about.  Both are re-exported
from ``start-instances.py``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from config import ActionConfig, InstanceConfig
from start_model import _XSSI_PREFIX

logger = logging.getLogger(__name__)

# Remote project lookup: host, API path, filter, cap, config to projects.
ProjectFetcher = Callable[[str, str, str, int, ActionConfig], list[str]]


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


def resolve_project_list(
    instance: InstanceConfig,
    api_path: str,
    config: ActionConfig,
    fetch: ProjectFetcher,
) -> list[str]:
    """Resolve the list of projects to pre-create.

    Handles three cases:
    1. No project filter — fetch all from remote.
    2. ``regex:`` prefix — fetch matching from remote.
    3. Literal name(s) — comma-separated list.

    *fetch* performs the remote lookup; ``start-instances.py`` supplies
    :func:`fetch_remote_projects`.
    """
    project = instance.project
    gerrit_host = instance.gerrit_host
    max_projects = instance.max_projects or config.max_projects

    if not project:
        # No filter — fetch everything
        logger.info("  No project filter, fetching full project list…")
        logger.info(
            "  (Max projects: %d — set MAX_PROJECTS env to override)",
            max_projects,
        )
        return fetch(gerrit_host, api_path, "", max_projects, config)

    if project.startswith("regex:"):
        regex_pattern = project[len("regex:") :]
        logger.info("  Project filter explicitly marked as regex: %s", regex_pattern)
        logger.info("  Fetching matching projects from remote…")
        return fetch(gerrit_host, api_path, regex_pattern, max_projects, config)

    # Literal project name(s) — comma-separated
    return [p.strip() for p in project.split(",") if p.strip()]


def precreate_projects(
    instance_dir: Path,
    raw_projects: list[str],
    chown: Callable[[Path], None],
) -> int:
    """Pre-create bare git repos for *raw_projects* under *instance_dir*.

    Gerrit's internal ``All-Projects`` / ``All-Users`` repositories are
    filtered out; the remaining count is written to
    ``expected_project_count`` for later verification by
    ``check-services`` / ``verify-replication`` and returned.

    *chown* is applied to each freshly created repository so the Gerrit
    container (UID:GID 1000:1000) can write to it.
    """
    filtered = [p for p in raw_projects if p not in ("All-Projects", "All-Users")]

    expected_count = len(filtered)
    logger.info(
        "  Found %d projects on remote server (excluding All-Projects/All-Users)",
        expected_count,
    )

    count_file = instance_dir / "expected_project_count"
    count_file.write_text(str(expected_count), encoding="utf-8")

    logger.info("  Pre-creating project directories for replication…")
    created = 0
    git_dir = instance_dir / "git"
    for proj in filtered:
        project_dir = git_dir / f"{proj}.git"
        if not project_dir.exists():
            project_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", str(project_dir)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            chown(project_dir)
            created += 1

    logger.info("  Created %d project directories", created)
    return expected_count
