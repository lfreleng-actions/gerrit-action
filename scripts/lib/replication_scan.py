# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Replication-error detection: patterns and the log scanner.

This module owns the rules that decide *what counts as a replication
error* and the scan that applies them:

- The regex catalogues for the authoritative per-event
  ``pull_replication_log`` and for the broader, noisier container
  log.
- The classification rules that downgrade known-benign plugin
  exceptions (soft failures) and Gerrit's magic repositories to
  non-fatal signals.
- :func:`check_replication_errors`, which runs both scans and returns
  a :class:`~replication_report.ReplicationErrorReport`.

The pattern catalogues live next to the scanner because they only
make sense together: each list encodes assumptions about the log
stream the scanner reads it against.
"""

from __future__ import annotations

import logging
import re

from docker_manager import DockerManager
from errors import DockerError
from replication_report import ErrorMatch, ReplicationErrorReport

logger = logging.getLogger(__name__)

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


def _scan_pull_replication_log(
    docker: DockerManager,
    cid: str,
    report: ReplicationErrorReport,
) -> None:
    """Scan the authoritative per-event log and record matches.

    Appends every matching line to ``report.log_file_matches``,
    classified by magic-repo and soft-failure status.  Failures to
    read the log are swallowed (logged at debug) because an absent or
    unreadable log is not itself a replication error.
    """
    try:
        # We use grep -E (one OR'd alternation) for speed inside
        # the container, then attribute each matched line back to
        # the specific pattern in Python so the report carries
        # accurate per-rule provenance.
        grep_pattern = "|".join(_REPLICATION_ERROR_PATTERNS)
        result = docker.exec_cmd(
            cid,
            f"tail -n 500 /var/gerrit/logs/pull_replication_log 2>/dev/null | "
            f"grep -iE '{grep_pattern}'",
            check=False,
        )
        # ``check_replication_errors`` scans matches in the order
        # ``grep`` emits them, which is the order they appear in
        # the log file.  Because every line in a Java stack trace
        # contains a class name with ``TransportException`` in it
        # (the plugin's own classes plus the ``Caused by:`` line),
        # grep returns every frame of a multi-line exception.  We
        # walk them in order, tag the headline by its exception
        # class, and propagate that classification onto the
        # subsequent stack frames / ``Caused by:`` lines until the
        # next headline resets the state.  Without this, the
        # generic ``PermanentTransportException.wrapIfPermanent…``
        # wrapper frame and the JGit ``Caused by:`` line of an
        # ``InexistentRefTransportException`` would each get tagged
        # as a separate hard failure and fail the workflow on
        # what is in fact a single soft exception.
        current_soft_state = False
        for line in result.splitlines():
            line = line.rstrip()
            if not line:
                continue
            matched_pattern = next(
                (
                    p
                    for p in _REPLICATION_ERROR_PATTERNS
                    if re.search(p, line, re.IGNORECASE)
                ),
                "|".join(_REPLICATION_ERROR_PATTERNS),
            )
            line_matches_soft = any(
                re.search(p, line, re.IGNORECASE) for p in _SOFT_FAILURE_PATTERNS
            )
            is_continuation = bool(_CONTINUATION_LINE_RE.match(line))
            if line_matches_soft:
                # Explicit soft-pattern match: this line itself
                # carries a known-soft exception class or the
                # JGit cause phrase.  Mark soft and update the
                # propagation state so any subsequent stack
                # frames inherit the flag.
                is_soft = True
                current_soft_state = True
            elif is_continuation:
                # Stack frame or ``Caused by:`` line: inherit the
                # most recent exception headline's classification.
                # If no headline has been seen yet (the scan
                # window started mid-trace), inherit ``False`` and
                # let the operator see the line under the
                # user-project heading; that is the conservative
                # default.
                is_soft = current_soft_state
            else:
                # Non-continuation, non-soft headline.  Reset the
                # propagation state so a subsequent stack frame
                # cannot inherit a stale soft flag from an
                # earlier exception in the same scan window.
                is_soft = False
                current_soft_state = False
            report.log_file_matches.append(
                ErrorMatch(
                    source="pull_replication_log",
                    pattern=matched_pattern,
                    line=line,
                    is_magic_repo=bool(_MAGIC_REPO_RE.search(line)),
                    is_soft_failure=is_soft,
                )
            )
    except DockerError as exc:
        logger.debug("Could not read pull_replication_log: %s", exc)


def _scan_container_logs(
    docker: DockerManager,
    cid: str,
    report: ReplicationErrorReport,
) -> None:
    """Scan ``docker logs`` output and record advisory matches.

    Note: this path is intentionally treated as a *secondary*
    signal.  Some failure modes (plugin-load errors, JGit
    ``TransportException`` stack traces that never reach the
    per-event log) only ever appear here, so we cannot drop the
    source entirely.  But its patterns must remain narrow because
    the underlying stream carries everything Gerrit logs.  The
    caller (verify_single_instance / wait_for_replication) is
    responsible for deciding whether to fail or merely warn on
    these matches — see ``has_advisory_errors``.
    """
    try:
        logs = docker.container_logs(cid, tail=2000)
        for pattern in _CONTAINER_ERROR_PATTERNS:
            for line in logs.splitlines():
                if re.search(pattern, line, re.IGNORECASE):
                    report.container_log_matches.append(
                        ErrorMatch(
                            source="container_logs",
                            pattern=pattern,
                            line=line.rstrip(),
                        )
                    )
    except DockerError as exc:
        logger.debug("Could not read container logs: %s", exc)


def check_replication_errors(
    docker: DockerManager,
    cid: str,
) -> ReplicationErrorReport:
    """Scan replication-related logs for known error patterns.

    Two sources are scanned independently:

    * The per-event ``pull_replication_log`` file inside the Gerrit
      container (the **authoritative** source — every line is
      replication-related, so a pattern hit reflects an actual
      replication failure).  Up to the last 500 lines are searched
      for ``_REPLICATION_ERROR_PATTERNS``.
    * ``docker logs`` against the Gerrit container (the **advisory**
      source — captures everything Gerrit writes to stdout/stderr;
      includes plugin loader output, JVM startup, web UI, email).
      Up to the last 2000 lines are searched for the narrower
      ``_CONTAINER_ERROR_PATTERNS``.

    Returns a :class:`ReplicationErrorReport` that records every
    matching line together with the source and the regex that fired.
    Callers decide how to react: typically failures only on
    ``has_authoritative_errors``, warnings on ``has_advisory_errors``.

    The previous boolean interface fused both sources and lost the
    per-source attribution, which led to false-positive failures
    when container-startup chatter happened to match the
    deliberately-broad ``pull-replication.*(error|failed|exception)``
    rule.  Returning a structured report removes the guesswork:
    every detection now carries the offending line, pattern, and
    source, ready for ``logger`` output.
    """
    report = ReplicationErrorReport()

    # --- 1. pull_replication_log (authoritative) ---
    if docker.exec_test(cid, "-f /var/gerrit/logs/pull_replication_log"):
        _scan_pull_replication_log(docker, cid, report)

    # --- 2. Container logs (narrow, replication-specific patterns only) ---
    _scan_container_logs(docker, cid, report)

    return report
