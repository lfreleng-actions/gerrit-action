# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Structured result types for replication-error scans.

This module owns the *classification vocabulary* of a replication
error scan: a single matched log line (:class:`ErrorMatch`) and the
per-source report that aggregates them
(:class:`ReplicationErrorReport`).

The report deliberately keeps the raw matches rather than collapsing
them to a boolean, so callers can apply different tolerance per
source (authoritative per-event log vs. advisory container log) and
per classification (user-project vs. magic-repo vs. known-benign
soft failure).  The scanner that produces these objects lives in
``replication_scan``; the operator-facing rendering lives in
``replication_diagnostics``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ErrorMatch:
    """A single line that matched a replication-error pattern.

    Attributes
    ----------
    source:
        Which log source the match came from.  One of
        ``"pull_replication_log"`` (the per-event log file Gerrit's
        pull-replication plugin writes) or ``"container_logs"`` (the
        Gerrit container's combined stdout/stderr captured via
        ``docker logs``).  Useful so callers can apply different
        tolerance to the authoritative per-event log vs. the broader
        heuristic container scan.
    pattern:
        The regex (string form) that matched the line.  Logged on
        every detection so a re-run never leaves operators guessing
        which rule fired.
    line:
        The matching line itself, with the trailing newline stripped.
    is_magic_repo:
        True when the matched line references one of Gerrit's special
        repositories (see ``_MAGIC_REPO_NAMES``).  These come from the
        opt-in ``<slug>-meta`` magic-repo remote that ``replicate_
        meta_refs`` enables.  Callers should treat these matches as
        a degraded-feature warning rather than a fatal replication
        failure — the source server's ACL on ``All-Users`` etc. is
        commonly stricter than its ACL on ordinary projects, and a
        permission denial there does not affect user-project
        replication.  See :class:`ReplicationErrorReport`'s
        ``has_user_project_errors`` / ``has_magic_repo_errors``
        properties for the structured accessors.
    is_soft_failure:
        True when the matched line also matches one of
        ``_SOFT_FAILURE_PATTERNS`` — known benign exception classes
        emitted by the pull-replication plugin (e.g.
        ``InexistentRefTransportException``).  Soft failures are
        excluded from every fatal-error gate; they only surface as
        warnings under their own heading so the operator can see
        them without the action stopping.  See
        :class:`ReplicationErrorReport.has_soft_failures`.
    """

    source: str
    pattern: str
    line: str
    is_magic_repo: bool = False
    is_soft_failure: bool = False


@dataclass
class ReplicationErrorReport:
    """Structured result from a replication-error scan.

    Separates the per-event log (authoritative) from the broader
    container log (advisory), so callers can choose tolerance per
    source instead of fusing them into a single bool that throws
    away which source and which pattern triggered.

    The previous ``check_replication_errors() -> bool`` interface
    made every detection a hard failure even when the only signal
    came from container-startup chatter that happened to match the
    deliberately-broad ``pull-replication.*(error|failed|exception)``
    pattern.  Callers can now distinguish authoritative replication
    failures (per-event log) from heuristic warnings (container log)
    and decide independently how to react.
    """

    log_file_matches: list[ErrorMatch] = field(default_factory=list)
    """Matches from ``/var/gerrit/logs/pull_replication_log``."""

    container_log_matches: list[ErrorMatch] = field(default_factory=list)
    """Matches from ``docker logs`` (container stdout/stderr)."""

    @property
    def has_authoritative_errors(self) -> bool:
        """True when the per-event replication log has matches.

        This is the high-confidence signal: every line in that file
        is replication-related, so a pattern hit there reflects an
        actual replication failure.
        """
        return bool(self.log_file_matches)

    @property
    def has_user_project_errors(self) -> bool:
        """True when the per-event log has matches against user projects.

        Excludes matches whose URL references one of Gerrit's magic
        repositories (``All-Users``, ``All-Projects``,
        ``All-External-IDs``, ``Sequences``) AND excludes matches
        flagged as soft failures (see ``has_soft_failures``).  This
        is the gate the verification callers use to decide whether
        to fail the workflow: user-project replication failures
        that are not known-benign soft failures are real problems
        and warrant aborting the deployment.
        """
        return any(
            not m.is_magic_repo and not m.is_soft_failure for m in self.log_file_matches
        )

    @property
    def has_magic_repo_errors(self) -> bool:
        """True when the per-event log has matches against magic repos.

        These come from the ``<slug>-meta`` remote that
        ``replicate_meta_refs`` enables.  A typical cause is the
        source server's stricter ACL on ``All-Users`` (which holds
        per-user PII) requiring admin-level read access that the
        replication service account does not have.  User-project
        replication is unaffected when only this property is true;
        the deployed Gerrit's UI just loses NoteDb account / group
        rendering for replicated changes.

        Soft failures (see ``has_soft_failures``) are excluded so
        they surface under their own heading and never inflate
        the magic-repo signal.
        """
        return any(
            m.is_magic_repo and not m.is_soft_failure for m in self.log_file_matches
        )

    @property
    def has_soft_failures(self) -> bool:
        """True when the per-event log has known-benign soft failures.

        Soft failures are pull-replication plugin exceptions whose
        meaning is informational rather than fatal.  Currently this
        is dominated by ``InexistentRefTransportException``, which
        the plugin raises when an explicitly-named refspec resolves
        to no advertised ref on the remote.  That happens routinely
        with the magic-repo remote's enumerated refspecs because:

        * The two magic projects (``All-Users`` and
          ``All-Projects``) have different ref sets; e.g.
          ``refs/meta/external-ids`` only lives on All-Users, so
          asking for it on All-Projects always raises this.
        * Source-server ACLs commonly hide certain refs (e.g.
          ``All-Users:refs/meta/external-ids``) from non-admin
          replication credentials; the smart-http advertisement
          simply omits them and the plugin treats the absence as a
          permanent failure.

        Neither case is something the action can fix — the right
        action is to surface the soft failures in the log and let
        replication continue.
        """
        return any(m.is_soft_failure for m in self.log_file_matches)

    @property
    def has_advisory_errors(self) -> bool:
        """True when the container log has matches.

        The container log captures everything Gerrit writes to
        stdout/stderr (plugin loader, JVM startup, web UI, email).
        Even with narrow patterns this source produces occasional
        false positives during startup.  Callers should surface
        these for diagnosis but not treat them as fatal on their
        own.
        """
        return bool(self.container_log_matches)

    @property
    def has_any_errors(self) -> bool:
        """True if either source produced at least one match."""
        return self.has_authoritative_errors or self.has_advisory_errors

    # ------------------------------------------------------------------
    # Diagnostic helpers — collapse the report into log lines the
    # caller can route to ``logger.warning`` / ``logger.error``.
    # ------------------------------------------------------------------

    def format_matches(
        self,
        *,
        max_per_source: int = 20,
        sources: tuple[str, ...] | None = None,
        magic_repo: bool | None = None,
        soft_failure: bool | None = None,
        only_lines: set[str] | None = None,
    ) -> list[str]:
        """Return human-readable lines describing matches.

        Each block starts with a heading that identifies the source
        and pattern, followed by up to *max_per_source* matching
        lines indented for readability.  Returns an empty list when
        no matches remain after filtering.

        Parameters
        ----------
        max_per_source:
            Truncate each per-pattern block to this many lines.
            Excess lines are summarised on a trailing
            ``… N more line(s) truncated`` row.
        sources:
            Restrict the output to matches whose ``source`` is in
            the given tuple (e.g. ``("pull_replication_log",)`` to
            exclude the container-log advisory matches).  ``None``
            (the default) includes every source.
        magic_repo:
            Restrict the output by magic-repo classification:
            ``True`` keeps only magic-repo matches (those whose
            ``is_magic_repo`` is True), ``False`` keeps only
            non-magic (user-project) matches, and ``None`` (the
            default) keeps both.
        soft_failure:
            Restrict the output by soft-failure classification:
            ``True`` keeps only soft failures (e.g.
            ``InexistentRefTransportException``), ``False`` keeps
            only non-soft (real) failures, and ``None`` (the
            default) keeps both.
        only_lines:
            Restrict the output to matches whose ``line`` text is
            in the given set.  ``None`` (the default) keeps every
            match.  Used by the wait-loop callers to pass a set of
            "newly-discovered" lines so the heading they print only
            contains those lines and not every match accumulated
            across the whole report — the per-loop dedup sets
            (``seen_advisory`` / ``seen_soft_failure`` /
            ``seen_magic_repo`` / ``seen_user_project``) gate the
            heading itself, and ``only_lines`` here scopes the body
            so each unique match is logged exactly once.

        Callers print warnings under four separate headings
        (advisory / soft-failure / magic-repo / user-project) and
        rely on these filters to keep the same line from appearing
        under more than one heading.
        """
        # Map source label → list of ``ErrorMatch`` objects, in the
        # order the caller normally prints them.  Filtering keeps
        # the ``ErrorMatch`` shape so we can inspect ``is_magic_repo``
        # per match, rather than the previous string-only buckets.
        source_buckets: tuple[tuple[str, str, list[ErrorMatch]], ...] = (
            (
                "pull_replication_log",
                "pull_replication_log (authoritative)",
                self.log_file_matches,
            ),
            (
                "container_logs",
                "container_logs (advisory)",
                self.container_log_matches,
            ),
        )

        out: list[str] = []
        for source_key, label, matches in source_buckets:
            if sources is not None and source_key not in sources:
                continue
            filtered = [
                m
                for m in matches
                if (magic_repo is None or m.is_magic_repo is magic_repo)
                and (soft_failure is None or m.is_soft_failure is soft_failure)
                and (only_lines is None or m.line in only_lines)
            ]
            if not filtered:
                continue
            # Group by pattern so callers can see which rule fired.
            by_pattern: dict[str, list[str]] = {}
            for m in filtered:
                by_pattern.setdefault(m.pattern, []).append(m.line)
            for pattern, lines in by_pattern.items():
                out.append(f"  {label} — pattern={pattern!r} — {len(lines)} match(es):")
                for line in lines[:max_per_source]:
                    out.append(f"    {line.rstrip()}")
                if len(lines) > max_per_source:
                    out.append(
                        f"    … {len(lines) - max_per_source} more line(s) truncated"
                    )
        return out
