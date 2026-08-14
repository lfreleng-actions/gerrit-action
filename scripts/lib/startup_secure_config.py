# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Writing replication credentials to ``secure.config``.

This module owns the only file in a Gerrit site that holds secrets: the
``secure.config`` consumed by the pull-replication plugin.  It is kept
separate from :mod:`startup_replication_config` so the handling of
credential material — which sections are emitted, and the file-mode
ordering that stops secrets from ever touching disk world-readable —
can be reviewed on its own.

The two modules must stay in lock-step: every ``[remote "…"]`` section
written into ``replication.config`` needs matching credentials here, or
the plugin silently falls back to anonymous auth and the source Gerrit
rejects the fetch.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import ActionConfig

logger = logging.getLogger(__name__)


def generate_secure_config(
    config_file: Path,
    slug: str,
    config: ActionConfig,
) -> None:
    """Generate ``secure.config`` with authentication credentials.

    Emits a per-remote section for every remote that
    ``generate_replication_config`` writes into the matching
    ``replication.config``.  When ``replicate_meta_refs`` is enabled
    that includes the magic-repo remote (``<slug>-meta``) targeting
    ``All-Users`` and ``All-Projects`` — without explicit credentials
    here the plugin falls back to anonymous auth, which the source
    Gerrit refuses with ``TransportException: not authorized``.
    """
    auth_type = config.auth_type.lower()

    if auth_type == "http_basic":
        sections = [
            f'[remote "{slug}"]\n'
            f"  username = {config.http_username}\n"
            f"  password = {config.http_password}\n"
        ]
        if config.replicate_meta_refs:
            # Mirror the credentials onto the magic-repo remote so
            # the matching ``[remote "<slug>-meta"]`` section emitted
            # by generate_replication_config can authenticate against
            # the source Gerrit when fetching All-Users / All-Projects.
            sections.append(
                f'[remote "{slug}-meta"]\n'
                f"  username = {config.http_username}\n"
                f"  password = {config.http_password}\n"
            )
        content = "".join(sections)
    elif auth_type == "bearer_token":
        # Bearer-token auth applies globally to all remotes via the
        # ``[auth]`` section, so no per-remote duplication is needed
        # for the magic-repo remote.
        content = f"[auth]\n  bearerToken = {config.bearer_token}\n"
    else:
        # SSH auth needs no credentials in secure.config, so the body
        # is empty.  The file itself is still created (empty, 0600)
        # below — only its credential contents are unnecessary here.
        content = ""

    config_file.parent.mkdir(parents=True, exist_ok=True)
    # Create (or truncate) secure.config with 0600 *before* writing
    # any credentials into it, so there is no transient window where
    # the file exists with a more permissive umask-derived mode
    # (e.g. 0644) while it already holds the HTTP password / bearer
    # token.  touch() creates an empty file; the explicit chmod also
    # tightens a pre-existing file (O_CREAT does not relax/alter the
    # mode of an existing file), and write_text() preserves the mode
    # of the now-existing file.
    config_file.touch(mode=0o600, exist_ok=True)
    config_file.chmod(0o600)
    config_file.write_text(content, encoding="utf-8")
