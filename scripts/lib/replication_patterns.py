# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tuning constants, log patterns and container commands.

Split out of :mod:`replication` so the probe, statistics and flow
modules share one definition of what counts as a replication error,
which repositories are Gerrit's own, and which shell commands are run
inside the container.  Every name here is re-exported from
:mod:`replication`.
"""

from __future__ import annotations

import re

# Minimum wait time for replication regardless of fetch interval
_MIN_WAIT_SECONDS = 60

# Default number of seconds with no state change before declaring
# replication "stable" (i.e. nothing new is being fetched).
_STABILITY_WINDOW_SECONDS = 45

# Minimum per-repo disk size in KB that indicates real content.
# An empty bare git repo is ~150 KB; anything above ~200 KB/repo
# means actual refs/objects were fetched.
_MIN_KB_PER_REPO = 200

# Patterns for detecting replication errors in the pull_replication_log.
# These are safe to use against the replication-specific log because every
# line in that file is replication-related.
_REPLICATION_ERROR_PATTERNS = [
    "Cannot replicate",
    "TransportException",
    "git-upload-pack not permitted",
    "Authentication.*failed",
    "Permission denied",
    "Connection refused",
]

# Patterns that match the pull-replication plugin's known *soft*
# failure modes.  When a line that already matched one of
# ``_REPLICATION_ERROR_PATTERNS`` ALSO matches one of these patterns,
# the resulting ``ErrorMatch`` is flagged ``is_soft_failure=True`` and
# excluded from the ``has_user_project_errors`` /
# ``has_magic_repo_errors`` failure gates.  Soft failures surface as a
# clearly-labelled warning block so the operator can see them in the
# workflow output without the action treating them as a fatal stop.
#
# Current entries:
#
# * ``InexistentRefTransportException`` — raised by the pull-
#   replication plugin when an explicitly-named refspec resolves to
#   no advertised ref on the remote.  This is expected in two
#   legitimate situations:
#
#   - The magic-repo remote's refspecs are heterogeneous across the
#     two magic projects ``All-Users`` and ``All-Projects``.  E.g.
#     ``refs/meta/external-ids`` only exists on All-Users; asking
#     for it on All-Projects raises this exception.  The blanket
#     ``+refs/meta/*:refs/meta/*`` wildcard used to mask this by
#     letting the plugin walk the remote's ref advertisement and
#     skip what isn't there, but the explicit refspec list we
#     adopted to keep ``refs/meta/config`` out of the wildcard
#     surface inevitably names refs that are absent from one or
#     other of the magic projects.  The exception is informational,
#     not a fault.
#
#   - The source server's ACL hides certain refs (notably
#     ``All-Users:refs/meta/external-ids``, which holds per-user
#     PII) from non-admin replication credentials.  From the plugin's
#     perspective the ref "does not exist" because the smart-http
#     refs advertisement does not list it; the underlying cause is
#     a missing read grant on the source.  Either way the right
#     action is the same: log it, do not fail the workflow, and let
#     the operator know the deployed Gerrit will run in a degraded-
#     NoteDb-rendering mode for that ref.
_SOFT_FAILURE_PATTERNS = [
    "InexistentRefTransportException",
    # The JGit-level cause line that pull-replication wraps into
    # ``InexistentRefTransportException``.  Appears on the ``Caused
    # by:`` line of the trace as e.g.::
    #
    #     Caused by: org.eclipse.jgit.errors.TransportException:
    #         Remote does not have refs/meta/external-ids available
    #         for fetch.
    #
    # Including the cause-line phrase here means the soft flag fires
    # on that line independently of the stateful stack-trace
    # propagation below, so a soft exception is still correctly
    # classified even if the headline is outside the 500-line scan
    # window.
    r"Remote does not have .* available for fetch",
]

# Stack-trace continuation lines.  Java exceptions span multiple
# lines: a headline (e.g. ``InexistentRefTransportException: ...``),
# zero or more ``\tat <FQN>(<file>:<line>)`` frames, and optionally
# a ``Caused by: <FQN>: ...`` line that introduces the next nested
# exception.  All these lines belong to the same logical exception
# and share its classification — a stack frame after a soft
# exception is itself a soft failure, even if the frame text alone
# doesn't mention the soft exception's class name.
#
# ``check_replication_errors`` uses this regex to identify
# continuation lines as it scans the grep output in order, and
# propagates the most recent headline's ``is_soft_failure`` flag
# onto them.  Without this propagation, the
# ``PermanentTransportException.wrapIfPermanentTransportException``
# wrapper frame and the ``Caused by: org.eclipse.jgit.errors.
# TransportException: ...`` line of an ``InexistentRefTransport``
# exception would each be classified as a separate hard
# user-project error and fail the workflow, even though they belong
# to the same logical soft failure as the headline.
_CONTINUATION_LINE_RE = re.compile(r"^\s*(?:at\s|Caused by:)")

# Patterns for detecting replication errors in the **container** logs.
#
# These must be much more selective than the pull_replication_log patterns
# because container logs contain ALL of Gerrit's output (web UI, email,
# account management, etc.).  Generic patterns like "Connection refused"
# or "Permission denied" cause false positives when e.g. the email
# subsystem cannot reach an SMTP server.
#
# All patterns require a replication verb (``fetch``, ``replicat``,
# ``remote``) *and* an error verb (``error``, ``failed``,
# ``exception``).  Mentioning the plugin name alone is not enough:
# ``Loaded plugin pull-replication, version v3.5.6`` and similar
# lifecycle lines never imply a fault, and the previous
# ``pull-replication.*(?:error|failed|exception)`` rule trip-fired
# whenever a plugin-loader / JVM-init message containing one of
# those bare words landed on the same line as the plugin name.
#
# Only patterns that unambiguously indicate a replication failure belong
# here.
_CONTAINER_ERROR_PATTERNS = [
    "Cannot replicate",
    # Plugin name + replication verb + error verb, in any order on the
    # same line.  The triple-anchor requirement keeps generic startup /
    # plugin-loader lines out of the false-positive surface.
    (
        r"pull-replication.*"
        r"(?:fetch|replicat|remote).*"
        r"(?:error|failed|exception)"
    ),
    (
        r"pull-replication.*"
        r"(?:error|failed|exception).*"
        r"(?:fetch|replicat|remote)"
    ),
    r"TransportException.*(?:fetch|replicate|remote)",
    "git-upload-pack not permitted",
]

# Pattern to extract unique completed repo names from pull_replication_log
# Log format: "[timestamp] [id] Replication from <url> completed in ..."
# URL formats:
#   - HTTPS: https://gerrit.example.org/r/a/<project>.git
#   - SSH:   ssh://gerrit.example.org:29418/<project>.git
#
# Extraction: strip prefix through "Replication from ", strip ".git completed..."
# suffix, strip /a/ path for HTTP, strip scheme://authority/ for SSH.
_COMPLETED_COUNT_CMD = (
    "grep 'Replication from .* completed' "
    "/var/gerrit/logs/pull_replication_log 2>/dev/null | "
    "sed -E '"
    "s|.*Replication from ||; "
    "s|\\.git completed.*||; "
    "s|.*/a/||; "
    "s|^[^:]+://[^/]+/||"
    "' | "
    "grep -v -E '^All-Projects$|^All-Users$' | "
    "sort -u | wc -l"
)

# Command to count bare git repos excluding system repos
_COUNT_REPOS_CMD = (
    "find /var/gerrit/git -name '*.git' -type d -prune 2>/dev/null | "
    "while read -r dir; do "
    '  if [ -f "$dir/HEAD" ]; then echo "$dir"; fi; '
    "done | "
    "grep -v -E 'All-Projects|All-Users' | wc -l"
)

# Command to get git directory disk usage in KB
_DISK_USAGE_CMD = "du -sk /var/gerrit/git 2>/dev/null | cut -f1"

# Command to get human-readable disk usage
_DISK_USAGE_HUMAN_CMD = "du -sh /var/gerrit/git 2>/dev/null | cut -f1"


# Gerrit special projects.  When ``replicate_meta_refs`` is enabled
# the action emits a second ``[remote "<slug>-meta"]`` section that
# targets these repositories with a broader refspec set.  Errors
# against them in the authoritative log are classified separately
# from user-project errors: the source server's ACL on All-Users
# typically requires admin scope (because it holds per-user PII),
# and a non-admin replication credential can fail there even when
# it has full read on every user project.  We never want a magic-
# repo permission denial alone to fail the workflow, because the
# core feature — per-project replication — still works in that
# case; the operator just loses NoteDb account/group rendering in
# the deployed Gerrit's UI.
_MAGIC_REPO_NAMES: tuple[str, ...] = (
    "All-Users",
    "All-Projects",
    "All-External-IDs",
    "Sequences",
)

# Pattern matched against an authoritative-log line to determine
# whether the offending fetch targeted a magic repository.  The
# pull-replication plugin writes both ``Cannot replicate from
# https://.../All-Users.git`` headlines and follow-on stack-trace
# lines such as ``TransportException: https://.../All-Users.git:
# not authorized``; either form is enough to attribute the match.
_MAGIC_REPO_RE = re.compile(
    r"/(" + "|".join(re.escape(name) for name in _MAGIC_REPO_NAMES) + r")\.git",
    re.IGNORECASE,
)
