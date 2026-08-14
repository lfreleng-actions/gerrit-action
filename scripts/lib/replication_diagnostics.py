# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Operator-facing rendering of replication-error reports.

Both the wait loop and the verification flow need to surface the
contents of a :class:`~replication_report.ReplicationErrorReport`
under four separate headings — advisory, soft-failure, magic-repo and
user-project — so an operator can tell at a glance which signals are
fatal and which are informational.  This module owns that rendering
so the two flows cannot drift apart in wording or ordering.

The wait loop additionally needs *deduplication*: it re-scans the logs
every poll interval and a persistent failure line would otherwise be
re-emitted ≈12 times a minute.  :class:`SeenMatchLines` carries the
per-loop state for that, and :func:`log_new_error_matches` only prints
lines that have not been seen before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from replication_report import ReplicationErrorReport

logger = logging.getLogger(__name__)

# Shared verbatim between the wait loop and the verification flow so
# the two never drift.  The remaining headings differ slightly (the
# verification wording adds "will not fail verification") and stay
# inline at their single call site.
_SOFT_FAILURE_HEADING = (
    "  Soft replication failures (refs missing on remote "
    "or hidden by source ACL; will not fail verification):"
)


@dataclass
class SeenMatchLines:
    """Per-wait-loop record of already-reported match lines.

    Track the set of already-warned diagnostic lines per source so we
    only log each unique advisory / magic-repo / user-project match
    once across the poll loop.  Without this guard the same
    ``Cannot replicate from ... All-Users.git`` line is re-emitted
    on every interval (≈12x per minute), drowning the legitimate
    progress lines and the final summary.
    """

    advisory: set[str] = field(default_factory=set)
    soft_failure: set[str] = field(default_factory=set)
    magic_repo: set[str] = field(default_factory=set)
    user_project: set[str] = field(default_factory=set)


def _log_new_block(
    emit: Callable[[str], None],
    heading: str,
    new_lines: list[str],
    report: ReplicationErrorReport,
    **filters: Any,
) -> None:
    """Emit *heading* plus the formatted body for *new_lines* only.

    ``format_matches`` is scoped with ``only_lines`` so the body
    contains just the newly-discovered lines rather than every match
    accumulated in the report; without that scoping a single new line
    on poll N would re-print every line seen on polls 1..N-1.
    """
    if not new_lines:
        return
    emit(heading)
    new_set = set(new_lines)
    for diag in report.format_matches(only_lines=new_set, **filters):
        emit(diag)


def log_new_error_matches(
    report: ReplicationErrorReport,
    seen: SeenMatchLines,
    *,
    debug: bool = False,
) -> None:
    """Log matches from *report* that *seen* has not recorded yet.

    Each heading is scoped to its own source / classification via
    ``format_matches()`` filters, so the same line never appears
    under more than one heading.  *seen* is mutated in place with
    every line that has now been reported.

    Advisory (container-log) signals are only emitted when *debug*
    is set: they are the noisiest source and never fatal.
    """
    if report.has_advisory_errors and debug:
        new_lines = [
            m.line for m in report.container_log_matches if m.line not in seen.advisory
        ]
        _log_new_block(
            logger.debug,
            "  Advisory replication signals (informational):",
            new_lines,
            report,
            sources=("container_logs",),
        )
        seen.advisory.update(new_lines)
    if report.has_soft_failures:
        # Soft failures (e.g. InexistentRefTransportException)
        # are surfaced under their own heading so the operator
        # knows the plugin tried to fetch a ref that didn't exist
        # or wasn't visible on the remote.  These never count
        # toward the failure threshold — they are an expected
        # consequence of the magic-repo remote's enumerated
        # refspec list spanning two heterogeneous magic projects
        # and tightly-ACL'd source servers.
        new_lines = [
            m.line
            for m in report.log_file_matches
            if m.is_soft_failure and m.line not in seen.soft_failure
        ]
        _log_new_block(
            logger.warning,
            _SOFT_FAILURE_HEADING,
            new_lines,
            report,
            sources=("pull_replication_log",),
            soft_failure=True,
        )
        seen.soft_failure.update(new_lines)
    if report.has_magic_repo_errors:
        new_lines = [
            m.line
            for m in report.log_file_matches
            if m.is_magic_repo
            and not m.is_soft_failure
            and m.line not in seen.magic_repo
        ]
        _log_new_block(
            logger.warning,
            "  Magic-repo replication errors (degraded NoteDb "
            "rendering; user-project replication unaffected):",
            new_lines,
            report,
            sources=("pull_replication_log",),
            magic_repo=True,
            soft_failure=False,
        )
        seen.magic_repo.update(new_lines)
    if report.has_user_project_errors:
        new_lines = [
            m.line
            for m in report.log_file_matches
            if not m.is_magic_repo
            and not m.is_soft_failure
            and m.line not in seen.user_project
        ]
        _log_new_block(
            logger.warning,
            "  Authoritative replication-log errors:",
            new_lines,
            report,
            sources=("pull_replication_log",),
            magic_repo=False,
            soft_failure=False,
        )
        seen.user_project.update(new_lines)


def log_non_fatal_matches(report: ReplicationErrorReport) -> None:
    """Warn about every advisory, soft and magic-repo match in *report*.

    Used by the one-shot verification scan, which — unlike the wait
    loop — runs once and therefore needs no deduplication.  None of
    these classifications fail verification; the wording says so
    explicitly so an operator reading the workflow log is not misled
    into thinking the run is doomed.
    """
    if report.has_advisory_errors:
        logger.warning(
            "  Advisory replication signals in container logs "
            "(informational, will not fail verification):"
        )
        for diag in report.format_matches(sources=("container_logs",)):
            logger.warning(diag)
    if report.has_soft_failures:
        logger.warning(_SOFT_FAILURE_HEADING)
        for diag in report.format_matches(
            sources=("pull_replication_log",), soft_failure=True
        ):
            logger.warning(diag)
    if report.has_magic_repo_errors:
        logger.warning(
            "  Magic-repo replication errors (degraded NoteDb "
            "rendering; user-project replication unaffected, "
            "will not fail verification):"
        )
        for diag in report.format_matches(
            sources=("pull_replication_log",),
            magic_repo=True,
            soft_failure=False,
        ):
            logger.warning(diag)


def log_user_project_matches(report: ReplicationErrorReport) -> None:
    """Report the fatal user-project matches in *report* as errors."""
    logger.error("Replication errors detected in pull_replication_log! ❌")
    for diag in report.format_matches(
        sources=("pull_replication_log",),
        magic_repo=False,
        soft_failure=False,
    ):
        logger.error(diag)


def log_indented_error_lines(text: str) -> None:
    """Log each line of *text* at error level, indented four spaces."""
    for line in text.splitlines():
        logger.error("    %s", line)


def log_indented_info_lines(text: str, indent: str = "  ") -> None:
    """Log each line of *text* at info level, prefixed with *indent*."""
    for line in text.splitlines():
        logger.info("%s%s", indent, line)
