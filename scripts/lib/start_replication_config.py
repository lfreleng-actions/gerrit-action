# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Generation of ``replication.config`` for the pull-replication plugin.

Split out of ``start-instances.py``.  The generated config uses
``fetchEvery`` for polling-based replication rather than ``apiUrl`` (the
two are mutually exclusive).  The magic-repo remote that mirrors
``All-Users`` / ``All-Projects`` lives in
:mod:`start_replication_remotes`; everything else — the source URL, the
refspec set and the per-project remote — is here.
:func:`generate_replication_config` is re-exported from
``start-instances.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import ActionConfig
from start_model import ReplicationOptions
from start_replication_remotes import magic_repo_remote_lines

logger = logging.getLogger(__name__)


def generate_replication_config(
    config_file: Path,
    options: ReplicationOptions,
) -> None:
    """Generate ``replication.config`` for the pull-replication plugin.

    The generated config uses ``fetchEvery`` for polling-based
    replication rather than ``apiUrl`` (the two are mutually exclusive).
    """
    config = options.config
    git_url = _build_git_url(options)
    sync_refs = _build_sync_refs(config)

    # Calculate connection timeout (at least 2 minutes, in milliseconds)
    timeout_ms = config.replication_timeout * 1000
    connection_timeout_ms = max(timeout_ms, 120_000)

    lines = _preamble_lines(config)
    lines.extend(
        _primary_remote_lines(options, git_url, sync_refs, connection_timeout_ms)
    )
    if config.replicate_meta_refs:
        lines.extend(magic_repo_remote_lines(options, git_url, connection_timeout_ms))

    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_git_url(options: ReplicationOptions) -> str:
    """Build the source URL the plugin fetches from."""
    gerrit_host = options.gerrit_host
    auth_type = options.config.auth_type.lower()

    # Build the git URL
    if auth_type == "ssh":
        git_url = (
            f"ssh://{options.remote_ssh_user}@{gerrit_host}"
            f":{options.remote_ssh_port}/${{name}}.git"
        )
    else:
        # HTTP-based auth uses /a/ prefix for authenticated access
        path = options.api_path.strip("/")
        if path:
            git_url = f"https://{gerrit_host}/{path}/a/${{name}}.git"
        else:
            git_url = f"https://{gerrit_host}/a/${{name}}.git"

    return git_url


def _build_sync_refs(config: ActionConfig) -> list[str]:
    """Build the refspec list the per-project remote fetches."""
    sync_refs = [r.strip() for r in config.sync_refs.split(",") if r.strip()]

    # When meta-ref replication is enabled, append the per-project
    # refspecs that carry NoteDb change metadata (``refs/changes/*``
    # already covers ``refs/changes/NN/CCCCCC/meta`` by hierarchy)
    # and per-project ACL / dashboard config (``refs/meta/config``,
    # ``refs/meta/dashboards`` etc.) plus the merge-preview cache.
    # (Account/group-scoped NoteDb refs such as
    # ``refs/meta/external-ids`` live on ``All-Users`` — not on
    # normal per-project repos — and are fetched by the dedicated
    # ``<slug>-meta`` remote, not by this per-project wildcard.)
    # Duplicates are tolerated by Gerrit but suppressed here so the
    # generated file stays tidy when an operator already lists the
    # ref pattern explicitly in ``sync_refs``.
    #
    # Note: the per-project remote DOES mirror each project's own
    # ``refs/meta/config`` (via the ``refs/meta/*`` wildcard).  This
    # is intentional and is a different trade-off from the magic-repo
    # remote's handling:
    #
    # * Per-project ``refs/meta/config`` defines that project's
    #   *project-local* ACL (per-ref read / push / submit
    #   permissions, plus the project's owner-group reference).
    # * The bootstrap container does not have a meaningful local
    #   ACL for replicated user projects (they were just pre-
    #   created as empty bare repos by ``fetch_and_precreate_
    #   projects``), so mirroring the source's ACL is a strict
    #   improvement: it lets the deployed Gerrit reflect the
    #   source server's per-project access rules.
    # * The bootstrap admin account 1000000 retains the global
    #   ``administrateServer`` capability (because the magic-repo
    #   remote excludes ``All-Projects:refs/meta/config`` — see the
    #   block comment further down), and that capability bypasses
    #   project-level ACLs, so the admin still sees every replicated
    #   project regardless of what its per-project ACL says.
    # * Non-admin / anonymous viewers may be blocked by per-project
    #   ACLs that reference source-server group UUIDs that do not
    #   exist locally, but that is the expected behaviour for a
    #   "mirror the source server" deployment shape.
    if config.replicate_meta_refs:
        for extra in (
            "+refs/meta/*:refs/meta/*",
            "+refs/cache-automerge/*:refs/cache-automerge/*",
        ):
            if extra not in sync_refs:
                sync_refs.append(extra)

    return sync_refs


def _preamble_lines(config: ActionConfig) -> list[str]:
    """Build the file header and the global ``[replication]`` block."""
    return [
        "# Pull-replication configuration",
        "# Auto-generated by gerrit-server-action",
        "#",
        "# This configuration uses fetchEvery for polling-based replication.",
        "# The plugin will poll the source Gerrit at the configured interval",
        "# to fetch any new or changed refs.",
        "",
        "[gerrit]",
        f"  replicateOnStartup = {str(config.sync_on_startup).lower()}",
        "  autoReload = true",
        "",
        "[replication]",
        "  lockErrorMaxRetries = 5",
        "  maxRetries = 5",
        "  useCGitClient = false",
        "  refsBatchSize = 50",
        "",
    ]


def _primary_remote_lines(
    options: ReplicationOptions,
    git_url: str,
    sync_refs: list[str],
    connection_timeout_ms: int,
) -> list[str]:
    """Build the ``[remote "<slug>"]`` section for user projects."""
    config = options.config
    project = options.project
    fetch_every_enabled = config.fetch_every_enabled
    fetch_interval = config.fetch_every

    lines = [
        f'[remote "{options.slug}"]',
        f"  url = {git_url}",
    ]

    if fetch_every_enabled:
        lines.append(f"  fetchEvery = {fetch_interval}")
        logger.info("  Fetch interval (polling): %s", fetch_interval)
    else:
        logger.info("  Automatic polling disabled (interval=%s)", fetch_interval)

    lines.extend(
        [
            f"  timeout = {config.replication_timeout}",
            f"  connectionTimeout = {connection_timeout_ms}",
            "  replicationDelay = 0",
            "  replicationRetry = 60",
            f"  threads = {config.replication_threads}",
            "  createMissingRepositories = true",
            "  replicateHiddenProjects = false",
        ]
    )

    logger.info("  Git URL for replication: %s", git_url)

    # Fetch refspecs
    for ref in sync_refs:
        lines.append(f"  fetch = {ref}")

    # Project filter
    if project:
        lines.append(f"  projects = {project}")

    # When meta-ref replication is on, the primary remote carries a
    # ``+refs/meta/*`` wildcard (appended above).  With an empty
    # ``project`` filter the primary remote replicates ALL projects
    # (including ``All-Projects`` and ``All-Users``); even with a
    # ``project`` filter set, an operator could write a pattern that
    # matches them.  In either case that wildcard could pull
    # ``All-Projects:refs/meta/config`` over the top of the deployed
    # container's locally-bootstrapped global ACL — the exact
    # Administrators-group hijack the magic-repo remote below goes to
    # great lengths to avoid.  The dedicated ``<slug>-meta`` remote is
    # the only path that should ever touch the magic projects (and it
    # enumerates NoteDb refs precisely so it never fetches
    # ``refs/meta/config``).  We therefore emit ``excludeProjects``
    # for both magic projects *unconditionally* whenever
    # ``replicate_meta_refs`` is enabled — independent of the
    # ``project`` filter — so the primary remote's wildcard can never
    # reach them.  The pull-replication plugin (a fork of the
    # ``replication`` plugin) supports ``excludeProjects`` for exactly
    # this: a project must match a ``projects`` pattern AND not match
    # any ``excludeProjects`` pattern to be replicated.
    if config.replicate_meta_refs:
        lines.append("  excludeProjects = All-Projects")
        lines.append("  excludeProjects = All-Users")

    return lines
