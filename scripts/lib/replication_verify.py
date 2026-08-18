# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Helpers for the replication *verification* phase.

``verify_single_instance`` and ``verify_all_instances`` in
:mod:`replication` run a fixed sequence of checks and then report on
them.  This module owns the self-contained pieces: the container
liveness pre-check, the per-step banner and evidence logging, and the
job-summary markdown emitted at the end of a run.

Nothing here decides pass/fail — that stays with the orchestrator so
the gating logic is visible in a single function.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import replication_diagnostics as diagnostics
from docker_manager import DockerManager
from errors import DockerError, ReplicationError
from replication_report import ReplicationErrorReport
from replication_state import VerificationResult

logger = logging.getLogger(__name__)


def log_verify_header(slug: str, cid: str, instance: dict[str, Any]) -> None:
    """Log the banner introducing a per-instance verification run."""
    gerrit_host = instance.get("gerrit_host", "")
    project = instance.get("project", "")

    logger.info("========================================")
    logger.info("Verifying replication: %s", slug)
    logger.info("========================================")
    logger.info("Container ID: %s", cid[:12] if cid else "(none)")
    logger.info("Source: %s", gerrit_host)
    if project:
        logger.info("Project filter: %s", project)
    logger.info("")


def container_state_error(docker: DockerManager, cid: str) -> str:
    """Return a reason string if the container is not usable, else "".

    Logs the failure (or the healthy state) as a side effect so the
    caller only has to propagate the message onto its result object.
    """
    try:
        if not docker.container_exists(cid):
            error = f"Container {cid[:12]} not found"
            logger.error("%s ❌", error)
            return error

        state = docker.container_state(cid)
        if state != "running":
            error = f"Container not running (state: {state})"
            logger.error("%s ❌", error)
            return error
        logger.info("Container state: %s ✅", state)
    except DockerError as exc:
        logger.error("Container check failed: %s", exc)
        return str(exc)
    return ""


def log_step(heading: str) -> None:
    """Log a blank separator followed by a numbered step heading."""
    logger.info("")
    logger.info(heading)


def log_config_content(config_content: str) -> None:
    """Echo the effective replication configuration, if any."""
    if config_content:
        logger.info("  Configuration content:")
        for line in config_content.splitlines():
            logger.info("    %s", line)


def log_error_evidence(report: ReplicationErrorReport, log_tail: str) -> str:
    """Report the fatal matches in *report* and return the failure reason.

    *log_tail* is dumped in full — it is the same 500-line window the
    scan itself uses, so the matching line is guaranteed to be in the
    dump even if the operator scrolls back from the ``format_matches``
    output to read the surrounding context.
    """
    diagnostics.log_user_project_matches(report)
    logger.error("  Pull replication log (last 500 lines):")
    diagnostics.log_indented_error_lines(log_tail)
    return "Replication errors detected"


def log_sample_repositories(docker: DockerManager, cid: str) -> None:
    """Log a handful of replicated repositories as proof of life."""
    logger.info("")
    logger.info("  Sample replicated repositories:")
    try:
        sample = docker.exec_cmd(
            cid,
            "find /var/gerrit/git -name '*.git' -type d -prune 2>/dev/null | "
            "grep -v 'All-Projects\\|All-Users' | head -5",
            check=False,
            timeout=10,
        )
        for line in sample.splitlines():
            logger.info("    %s", line)
    except DockerError as exc:
        logger.debug("Could not list sample replicated repositories: %s", exc)


def log_recent_replication_logs(docker: DockerManager, cid: str) -> None:
    """Dump the tail of replication-related container log lines."""
    try:
        container_logs = docker.container_logs(cid, tail=3000)
        repl_lines = [
            line
            for line in container_logs.splitlines()
            if re.search(
                r"replication|pull-replication|fetch|remote",
                line,
                re.IGNORECASE,
            )
        ]
        if repl_lines:
            logger.error("  Recent replication logs:")
            for line in repl_lines[-20:]:
                logger.error("    %s", line.strip())
    except DockerError as log_exc:
        logger.debug("Could not retrieve replication logs: %s", log_exc)


def log_final_stats(result: VerificationResult) -> None:
    """Log the closing statistics for one successfully verified instance.

    The 5 % tolerance on the expected project count absorbs projects
    that exist on the source but are legitimately not replicated
    (hidden, read-restricted, or created mid-run).
    """
    expected_count = result.expected_count
    logger.info("  Replicated repositories: %d", result.repo_count)
    if expected_count > 0:
        logger.info("  Expected from remote: %d", expected_count)

        # Validate count with tolerance
        min_required = expected_count * 95 // 100
        if result.repo_count >= min_required:
            logger.info("  ✅ Project count matches expected (within 5%% tolerance)")
        elif expected_count > 0:
            pct = result.repo_count * 100 // expected_count
            logger.warning(
                "  ⚠️ Project count mismatch: got %d%% of expected projects",
                pct,
            )

    logger.info("  Disk usage: %s", result.disk_usage)
    logger.info("")
    logger.info("✅ Instance %s verification passed", result.slug)
    logger.info("")


def log_verification_banner(total: int, failed_count: int) -> None:
    """Log the cross-instance verification summary banner."""
    logger.info("========================================")
    logger.info("Verification Summary")
    logger.info("========================================")
    logger.info("Total instances: %d", total)
    logger.info("Failed: %d", failed_count)
    logger.info("")


def log_disk_usage_summary(results: list[VerificationResult]) -> None:
    """Log per-instance disk usage after a fully successful run."""
    logger.info("========================================")
    logger.info("Disk Usage Summary")
    logger.info("========================================")
    for r in results:
        logger.info("")
        logger.info("Instance: %s", r.slug)
        logger.info("  Disk usage: %s", r.disk_usage)
    logger.info("")


def verification_success_summary(results: list[VerificationResult]) -> str:
    """Job-summary markdown for a fully successful verification run."""
    summary_lines = [
        "## Replication Verification ✅",
        "",
        "All instances successfully replicated from source Gerrit servers.",
        "",
        "### Instance Details",
        "",
        "| Instance | Repos | Expected | Disk Usage |",
        "|----------|-------|----------|------------|",
    ]
    for r in results:
        expected_display = str(r.expected_count) if r.expected_count > 0 else "N/A"
        summary_lines.append(
            f"| {r.slug} | {r.repo_count} | {expected_display} | {r.disk_usage} |"
        )
    summary_lines.append("")
    return "\n".join(summary_lines)


def verification_failure_error(failed: list[VerificationResult]) -> ReplicationError:
    """Build the aggregate failure raised when instances did not verify."""
    slugs = ", ".join(r.slug for r in failed)
    return ReplicationError(
        f"Replication verification failed for: {slugs}",
        expected_count=sum(r.expected_count for r in failed),
        actual_count=sum(r.repo_count for r in failed),
    )


def verification_failure_summary(failed_count: int, total: int) -> str:
    """Job-summary markdown for a run where at least one instance failed."""
    lines = [
        "**Replication Verification** ❌",
        "",
        f"{failed_count} of {total} instances failed verification.",
        "",
        "Check the workflow logs for detailed error information.",
        "",
    ]
    return "\n".join(lines)
