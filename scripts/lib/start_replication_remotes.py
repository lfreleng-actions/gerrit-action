# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The magic-repo remote of ``replication.config``.

Split out of :mod:`start_replication_config`.  When meta-ref
replication is enabled this module contributes a second remote that
targets Gerrit's ``All-Users`` and ``All-Projects`` repositories, and
the block comment below records why its refspecs are enumerated by
hand rather than wildcarded.
"""

from __future__ import annotations

import logging

from start_model import ReplicationOptions

logger = logging.getLogger(__name__)


# When meta-ref replication is enabled, emit a second remote that
# targets Gerrit's ``All-Users`` and ``All-Projects`` repositories
# with the broader set of refspecs they need.  These are the
# "magic" projects that hold per-account identities, external IDs,
# group membership, the change-number sequence and global ACLs.
# Without them, replicated changes display authors as
# "Gerrit Code Review", group ACLs do not resolve, and any new
# change uploaded against the CI Gerrit risks colliding with the
# source's change-number sequence.
#
# We use a distinct ``[remote "<slug>-meta"]`` section so the
# primary remote's project filter and per-project refspecs stay
# narrowly scoped to user projects.  ``createMissingRepositories``
# is set to ``false`` here because Gerrit always creates these
# special repos itself during ``gerrit init``.
#
# CRITICAL: refs/meta/config is INTENTIONALLY EXCLUDED.
# ====================================================
# An earlier version of this code used a blanket
# ``+refs/meta/*:refs/meta/*`` refspec for both magic projects.
# That pulled the source server's
# ``All-Projects:refs/meta/config`` over the top of the deployed
# container's locally-bootstrapped global ACL.  The fallout was
# silent but severe:
#
# * ``refs/meta/config`` on ``All-Projects`` is the
#   server-wide ACL definition.  It names the UUID of the
#   ``Administrators`` group, plus every per-ref permission
#   block (read / push / submit / forge-author / etc.).
# * The bootstrap container creates account 1000000 and adds
#   it to the *local* Administrators group (with a locally
#   generated UUID).  Setup-gerrit-user.py then provisions the
#   operator's SSH keys against that account.
# * After pull-replication writes the source server's
#   ``refs/meta/config`` into the local All-Projects, the
#   Administrators group reference resolves to the source
#   server's UUID, whose membership is the source server's
#   admins — NOT the local account 1000000.  Account 1000000
#   still exists in ``refs/users/00/1000000`` (untouched
#   because the ``refs/users/*`` fetch typically fails on
#   ACL-restricted source servers), but it is no longer in
#   any group with ``administrateServer`` capability.
# * Every REST endpoint that requires ``administrate-server``
#   or ``maintain-server`` then returns 403 against the
#   would-be admin.  ``POST /projects/<name>/index.changes``
#   needs ``administrate-server``; ``POST /config/server/
#   caches/<name>/flush`` needs ``maintain-server``.  Both
#   of those calls fired 403 across the board in the
#   2026-05-21 14:35 / 14:51 / 15:02 dispatches with the
#   blanket-refspec config.
# * Operators see the symptom in the deployed Gerrit UI too:
#   their account exists (DEVELOPMENT_BECOME_ANY_ACCOUNT
#   still works) but the admin UI is locked because the
#   Administrators group no longer points at them.
#
# Fix: enumerate the meta refs we actually need by exact name
# so the wildcard never grabs ``refs/meta/config``.  The
# specific refs we want are NoteDb-related (the things that
# actually let the deployed Gerrit render the source server's
# accounts / groups / changes correctly):
#
# * ``refs/meta/external-ids`` (All-Users) — login → account_id
#   map; without this, replicated changes show ``Anonymous
#   Coward`` instead of real author names.
# * ``refs/meta/group-names`` (All-Users) — group UUID → name
#   map; without this, replicated group references render as
#   raw UUIDs.
# * ``refs/meta/version`` (both) — NoteDb schema version pin;
#   harmless to mirror and required for some consistency
#   checks the secondary index runs.
#
# We do NOT enumerate ``refs/meta/config`` for either project
# because:
#   - All-Projects:refs/meta/config = source's global ACL
#     (the hijack risk above).
#   - All-Users:refs/meta/config = source's All-Users-specific
#     ACL; replicating it has the same hijack effect against
#     account-edit / draft-comment permissions on the local
#     deployment.


def magic_repo_remote_lines(
    options: ReplicationOptions,
    git_url: str,
    connection_timeout_ms: int,
) -> list[str]:
    """Build the ``[remote "<slug>-meta"]`` section.

    Returns the lines for the magic-repo remote; the caller only emits
    them when ``replicate_meta_refs`` is enabled.
    """
    config = options.config
    fetch_every_enabled = config.fetch_every_enabled
    fetch_interval = config.fetch_every

    lines = _magic_remote_header_lines(options.slug, git_url)
    if fetch_every_enabled:
        lines.append(f"  fetchEvery = {fetch_interval}")
    lines.extend(
        [
            f"  timeout = {config.replication_timeout}",
            f"  connectionTimeout = {connection_timeout_ms}",
            "  replicationDelay = 0",
            "  replicationRetry = 60",
            f"  threads = {config.replication_threads}",
            "  createMissingRepositories = false",
            "  replicateHiddenProjects = true",
            # Refspecs are enumerated rather than wildcarded so
            # the source server's ``refs/meta/config`` (on either
            # All-Projects or All-Users) is never pulled.  See the
            # block comment above for the rationale.  Each refspec
            # is documented inline so future maintainers can see
            # which magic project the ref normally lives on and
            # why we are mirroring it.
            #
            # NoteDb identity / membership refs (All-Users):
            "  fetch = +refs/users/*:refs/users/*",
            "  fetch = +refs/groups/*:refs/groups/*",
            "  fetch = +refs/meta/external-ids:refs/meta/external-ids",
            "  fetch = +refs/meta/group-names:refs/meta/group-names",
            # Per-user state refs (All-Users); harmless no-ops
            # against All-Projects because they don't exist there.
            "  fetch = +refs/draft-comments/*:refs/draft-comments/*",
            "  fetch = +refs/starred-changes/*:refs/starred-changes/*",
            # Change-number sequence (All-Projects); also a no-op
            # against All-Users.  Without this the local container
            # would start handing out change numbers from 1, which
            # would collide with the replicated refs/changes/*
            # numbers on the very first locally-uploaded change.
            # The test Gerrit is read-only by policy, so this is
            # belt-and-braces, but cheap to mirror.
            "  fetch = +refs/sequences/*:refs/sequences/*",
            # NoteDb schema-version pin (both projects).  Cheap to
            # mirror and helps some consistency-check paths in the
            # secondary index.
            "  fetch = +refs/meta/version:refs/meta/version",
            # Magic-project filter.  Both names are listed even
            # though some refspecs only apply to one of them —
            # the pull-replication plugin silently skips refs
            # that don't exist for a given project.
            "  projects = All-Users",
            "  projects = All-Projects",
        ]
    )
    logger.info(
        "  Meta-ref replication enabled: "
        "All-Users / All-Projects NoteDb refs will be mirrored "
        "(refs/meta/config excluded to preserve local ACL)"
    )
    return lines


def _magic_remote_header_lines(slug: str, git_url: str) -> list[str]:
    """Build the magic-repo remote's banner comment and URL."""
    return [
        "",
        "# Magic-repo remote: All-Users / All-Projects NoteDb refs.",
        "# Required for accounts, external IDs, groups and the",
        "# change-number sequence to resolve on the deployed",
        "# CI Gerrit.  refs/meta/config is INTENTIONALLY",
        "# EXCLUDED — see the source comment in",
        "# generate_replication_config for the full rationale",
        "# (TL;DR: replicating it overwrites the local",
        "# Administrators-group ACL and breaks reindex /",
        "# cache-flush / admin UI on the bootstrap account).",
        f'[remote "{slug}-meta"]',
        f"  url = {git_url}",
    ]
