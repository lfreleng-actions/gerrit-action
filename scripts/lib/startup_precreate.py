# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Pre-creating the repositories that replication will then fill.

The pull-replication plugin's ``fetchEvery`` mode only polls
repositories that Gerrit already knows about, so every project expected
from the source server must exist as a bare git repository *before* the
container starts.  This module owns that step:

- :func:`resolve_project_list` — turn the instance's ``project`` filter
  (empty, ``regex:``-prefixed, or a literal comma-separated list) into
  concrete project names, querying the source server when needed.
- :func:`fetch_and_precreate_projects` — create the bare repositories,
  and record the expected count for the later verification steps.

Gerrit's own ``All-Projects``/``All-Users`` repositories are excluded:
they are created by ``gerrit init`` and are never replicated.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from config import ActionConfig, InstanceConfig
from startup_site_layout import chown_tree
from startup_source_projects import fetch_remote_projects

logger = logging.getLogger(__name__)

# Gerrit-internal repositories that must never be pre-created or counted
_SYSTEM_PROJECTS = ("All-Projects", "All-Users")


def resolve_project_list(
    instance: InstanceConfig,
    api_path: str,
    config: ActionConfig,
) -> list[str]:
    """Resolve the list of projects to pre-create.

    Handles three cases:
    1. No project filter — fetch all from remote.
    2. ``regex:`` prefix — fetch matching from remote.
    3. Literal name(s) — comma-separated list.
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
        return fetch_remote_projects(gerrit_host, api_path, "", max_projects, config)

    if project.startswith("regex:"):
        regex_pattern = project[len("regex:") :]
        logger.info("  Project filter explicitly marked as regex: %s", regex_pattern)
        logger.info("  Fetching matching projects from remote…")
        return fetch_remote_projects(
            gerrit_host, api_path, regex_pattern, max_projects, config
        )

    # Literal project name(s) — comma-separated
    return [p.strip() for p in project.split(",") if p.strip()]


def fetch_and_precreate_projects(
    instance_dir: Path,
    instance: InstanceConfig,
    api_path: str,
    config: ActionConfig,
) -> int:
    """Fetch expected projects and pre-create bare git repos.

    Pre-creation is **required** because the ``fetchEvery`` mode only
    polls repositories that already exist in Gerrit's ``projectCache``.
    Without pre-creating the directories the plugin will not know about
    them and will not fetch.

    Returns the expected project count (excluding system repos).
    """
    logger.info("Fetching expected project count from remote…")

    raw_projects = resolve_project_list(instance, api_path, config)

    # Filter out Gerrit internal/system projects
    filtered = [p for p in raw_projects if p not in _SYSTEM_PROJECTS]

    expected_count = len(filtered)
    logger.info(
        "  Found %d projects on remote server (excluding All-Projects/All-Users)",
        expected_count,
    )

    # Store for later verification by check-services / verify-replication
    count_file = instance_dir / "expected_project_count"
    count_file.write_text(str(expected_count), encoding="utf-8")

    # Pre-create bare repositories
    logger.info("  Pre-creating project directories for replication…")
    created = _precreate_bare_repos(instance_dir / "git", filtered)

    logger.info("  Created %d project directories", created)
    return expected_count


def _precreate_bare_repos(git_dir: Path, projects: list[str]) -> int:
    """Create a bare repository per project, returning how many were new."""
    created = 0
    for proj in projects:
        project_dir = git_dir / f"{proj}.git"
        if not project_dir.exists():
            project_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", str(project_dir)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            chown_tree(project_dir)
            created += 1
    return created
