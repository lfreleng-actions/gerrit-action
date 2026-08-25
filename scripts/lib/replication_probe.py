# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Replication configuration checks and log-error scanning.

Split out of :mod:`replication`; these probes read the replication
configuration out of a running container and scan both the
pull-replication log and the container log for known error patterns.
Every name here is re-exported from :mod:`replication`.
"""

from __future__ import annotations

import logging
import re

from docker_manager import DockerManager
from errors import DockerError
from replication_patterns import (
    _CONTAINER_ERROR_PATTERNS,
    _CONTINUATION_LINE_RE,
    _MAGIC_REPO_RE,
    _REPLICATION_ERROR_PATTERNS,
    _SOFT_FAILURE_PATTERNS,
)
from replication_report import ErrorMatch, ReplicationErrorReport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plugin and configuration checks
# ---------------------------------------------------------------------------


def check_replication_config(docker: DockerManager, cid: str) -> bool:
    """Verify that ``replication.config`` exists in the container.

    Returns *True* if the file exists.
    """
    result: bool = docker.exec_test(cid, "-f /var/gerrit/etc/replication.config")
    return result


def show_replication_config(docker: DockerManager, cid: str) -> str:
    """Read and return the replication config (excluding comments/blanks).

    Returns the config content or an empty string.
    """
    try:
        raw = docker.exec_cmd(
            cid,
            "cat /var/gerrit/etc/replication.config 2>/dev/null",
            check=False,
        )
        # Filter out comments and blank lines
        lines = [
            line
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return "\n".join(lines)
    except DockerError:
        return ""


def check_secure_config(docker: DockerManager, cid: str) -> bool:
    """Check if secure.config exists and log its sections.

    Returns *True* if the file exists.
    """
    if not docker.exec_test(cid, "-f /var/gerrit/etc/secure.config"):
        logger.warning("secure.config not found")
        return False

    logger.info("  secure.config exists ✅")
    try:
        sections = docker.exec_cmd(
            cid,
            "grep '^\\[' /var/gerrit/etc/secure.config 2>/dev/null",
            check=False,
        )
        if sections:
            logger.info("  secure.config sections:")
            for line in sections.splitlines():
                logger.info("    %s", line)
    except DockerError as exc:
        logger.debug("Could not read secure.config sections: %s", exc)
    return True


# ---------------------------------------------------------------------------
# Replication log analysis
# ---------------------------------------------------------------------------


def classify_log_matches(text: str) -> list[ErrorMatch]:
    """Turn matched ``pull_replication_log`` lines into classified records.

    *text* is the raw output of a ``grep -iE`` over the log with the
    ``_REPLICATION_ERROR_PATTERNS`` alternation, so the lines arrive in
    the order they appear in the file.  Because every line of a Java
    stack trace contains a class name with ``TransportException`` in it
    (the plugin's own classes plus the ``Caused by:`` line), grep
    returns every frame of a multi-line exception.  We walk them in
    order, tag the headline by its exception class and target
    repository, and propagate that classification onto the subsequent
    stack frames / ``Caused by:`` lines until the next headline resets
    the state.

    Both flags are propagated for the same reason.  Without it the
    generic ``PermanentTransportException.wrapIfPermanent…`` wrapper
    frame and the JGit ``Caused by:`` line of an
    ``InexistentRefTransportException`` would each get tagged as a
    separate hard failure and fail the workflow on what is in fact a
    single soft exception; and the frames of an ``All-Users.git`` fetch
    failure — which name a class and method but not the repository URL
    — would be scored as user-project errors even though the headline
    they belong to targets a magic repository.

    Returns one :class:`ErrorMatch` per non-blank line, in input order.
    """
    matches: list[ErrorMatch] = []
    current_soft_state = False
    current_magic_state = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
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
        line_matches_magic = bool(_MAGIC_REPO_RE.search(line))

        if _CONTINUATION_LINE_RE.match(line):
            # Stack frame or ``Caused by:`` line: inherit the most
            # recent exception headline's classification unless the
            # frame itself carries the evidence.  If no headline has
            # been seen yet (the scan window started mid-trace), the
            # inherited value is False and the operator sees the line
            # under the user-project heading; that is the conservative
            # default.
            is_soft = line_matches_soft or current_soft_state
            is_magic = line_matches_magic or current_magic_state
            current_soft_state = is_soft
            current_magic_state = is_magic
        else:
            # Headline: it alone decides the classification, and
            # resets the propagation state so a subsequent stack frame
            # cannot inherit a stale flag from an earlier exception in
            # the same scan window.
            is_soft = line_matches_soft
            is_magic = line_matches_magic
            current_soft_state = is_soft
            current_magic_state = is_magic

        matches.append(
            ErrorMatch(
                source="pull_replication_log",
                pattern=matched_pattern,
                line=line,
                is_magic_repo=is_magic,
                is_soft_failure=is_soft,
            )
        )

    return matches


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
            report.log_file_matches.extend(classify_log_matches(result))
        except DockerError as exc:
            logger.debug("Could not read pull_replication_log: %s", exc)

    # --- 2. Container logs (narrow, replication-specific patterns only) ---
    #
    # Note: this path is intentionally treated as a *secondary*
    # signal.  Some failure modes (plugin-load errors, JGit
    # ``TransportException`` stack traces that never reach the
    # per-event log) only ever appear here, so we cannot drop the
    # source entirely.  But its patterns must remain narrow because
    # the underlying stream carries everything Gerrit logs.  The
    # caller (verify_single_instance / wait_for_replication) is
    # responsible for deciding whether to fail or merely warn on
    # these matches — see ``has_advisory_errors``.
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

    return report
