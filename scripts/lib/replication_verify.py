# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Per-instance replication verification flow.

Split out of :mod:`replication`, which re-exports
:func:`verify_single_instance`.  The five numbered steps of
``verify-replication.sh`` each get their own helper here so the entry
point reads as the sequence it describes.

The steps here reach their collaborators through the :mod:`replication`
facade rather than importing them directly.  Every probe in this
package is re-exported there, and callers (the test suite in
particular) rebind those attributes to stub out a step; resolving the
name on the facade at call time is what keeps that substitution
working now the probes live in sibling modules.  :mod:`replication`
imports this module for its own re-exports, so the import below is
deliberately circular; only attribute lookups happen at call time,
never at import time.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import replication
from docker_manager import DockerManager
from errors import DockerError, ReplicationError
from replication_model import VerificationResult
from replication_patterns import _STABILITY_WINDOW_SECONDS
from replication_report import ReplicationErrorReport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verification steps
# ---------------------------------------------------------------------------


def _log_verify_banner(cid: str, slug: str, instance: dict[str, Any]) -> None:
    """Announce which instance is about to be verified."""
    project = instance.get("project", "")
    logger.info("========================================")
    logger.info("Verifying replication: %s", slug)
    logger.info("========================================")
    logger.info("Container ID: %s", cid[:12] if cid else "(none)")
    logger.info("Source: %s", instance.get("gerrit_host", ""))
    if project:
        logger.info("Project filter: %s", project)
    logger.info("")


def _verify_container_running(
    docker: DockerManager,
    cid: str,
    result: VerificationResult,
) -> bool:
    """Confirm the container exists and is running, recording why not."""
    try:
        if not docker.container_exists(cid):
            result.error = f"Container {cid[:12]} not found"
            logger.error("%s ❌", result.error)
            return False

        state = docker.container_state(cid)
        if state != "running":
            result.error = f"Container not running (state: {state})"
            logger.error("%s ❌", result.error)
            return False
        logger.info("Container state: %s ✅", state)
    except DockerError as exc:
        result.error = str(exc)
        logger.error("Container check failed: %s", exc)
        return False
    return True


def _verify_replication_setup(
    docker: DockerManager,
    cid: str,
    result: VerificationResult,
) -> bool:
    """Run steps 1, 2 and 2b: plugin, ``replication.config``, credentials."""
    logger.info("")
    logger.info("Step 1: Verifying pull-replication plugin…")
    if not replication.verify_plugin_loaded(docker, cid, "pull-replication"):
        result.error = "Pull-replication plugin not loaded"
        logger.error("%s ❌", result.error)
        return False

    logger.info("")
    logger.info("Step 2: Verifying replication configuration…")
    if replication.check_replication_config(docker, cid):
        logger.info("  replication.config found ✅")
        config_content = replication.show_replication_config(docker, cid)
        if config_content:
            logger.info("  Configuration content:")
            for line in config_content.splitlines():
                logger.info("    %s", line)
    else:
        result.error = "replication.config not found"
        logger.error("%s ❌", result.error)
        return False

    logger.info("")
    logger.info("Step 2b: Verifying authentication configuration…")
    replication.check_secure_config(docker, cid)
    return True


def _report_error_scan(
    docker: DockerManager,
    cid: str,
    report: ReplicationErrorReport,
    result: VerificationResult,
) -> bool:
    """Log what the error scan found; return *False* only for real failures.

    Failure gating is identical to ``wait_for_replication``'s step 1:
    only authoritative user-project errors can fail verification.
    Magic-repo failures (``All-Users`` etc.) and container-log
    advisory signals surface as warnings so the operator sees them
    without the action bailing on environmental ACL restrictions
    or startup chatter.
    """
    if report.has_advisory_errors:
        logger.warning(
            "  Advisory replication signals in container logs "
            "(informational, will not fail verification):"
        )
        for diag in report.format_matches(sources=("container_logs",)):
            logger.warning(diag)
    if report.has_soft_failures:
        logger.warning(
            "  Soft replication failures (refs missing on remote "
            "or hidden by source ACL; will not fail verification):"
        )
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
    if report.has_user_project_errors:
        logger.error("Replication errors detected in pull_replication_log! ❌")
        for diag in report.format_matches(
            sources=("pull_replication_log",),
            magic_repo=False,
            soft_failure=False,
        ):
            logger.error(diag)
        result.error = "Replication errors detected"
        # Dump the full 500-line tail — same window the scan uses,
        # so the matching line is guaranteed to be in the dump even
        # if the operator scrolls back from the format_matches
        # output to the surrounding context.
        log_tail = replication.show_pull_replication_log(docker, cid, lines=500)
        logger.error("  Pull replication log (last 500 lines):")
        for line in log_tail.splitlines():
            logger.error("    %s", line)
        return False

    logger.info("  No replication errors detected ✅")
    return True


def _log_sample_repositories(docker: DockerManager, cid: str) -> None:
    """Show a handful of the repositories that arrived."""
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


def _log_replication_failure(docker: DockerManager, cid: str) -> None:
    """Echo the replication-related tail of the container log."""
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


def _log_final_stats(
    docker: DockerManager,
    cid: str,
    result: VerificationResult,
    expected_count: int,
) -> None:
    """Record step 5: how much was replicated, against what was expected."""
    logger.info("")
    logger.info("Step 5: Final replication statistics…")
    result.repo_count = replication.count_repositories(docker, cid)
    result.completed_count = replication.get_completed_repo_count(docker, cid)
    result.disk_usage = replication.get_git_disk_usage_human(docker, cid)
    result.disk_usage_mb = replication.get_git_disk_usage_mb(docker, cid)

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


# ---------------------------------------------------------------------------
# Verify a single instance
# ---------------------------------------------------------------------------


def verify_single_instance(
    docker: DockerManager,
    slug: str,
    instance: dict[str, Any],
    timeout: int = 180,
    debug: bool = False,
    stability_window: int = _STABILITY_WINDOW_SECONDS,
) -> VerificationResult:
    """Verify replication for a single instance.

    This runs the full verification flow from ``verify-replication.sh``:
    1. Verify plugin loaded
    2. Verify replication config
    3. Check for errors
    4. Wait for replication
    5. Report final stats
    """
    result = VerificationResult(slug=slug)

    cid = instance.get("cid", "")
    project = instance.get("project", "")
    expected_count = int(instance.get("expected_project_count", 0))
    result.expected_count = expected_count

    _log_verify_banner(cid, slug, instance)

    if not _verify_container_running(docker, cid, result):
        return result
    if not _verify_replication_setup(docker, cid, result):
        return result

    logger.info("")
    logger.info("Step 3: Checking for replication errors…")
    error_report = replication.check_replication_errors(docker, cid)
    if not _report_error_scan(docker, cid, error_report, result):
        return result

    logger.info("")
    logger.info("Step 4: Waiting for replicated repositories…")
    try:
        replication.wait_for_replication(
            docker,
            cid,
            slug,
            timeout=timeout,
            expected_count=expected_count,
            project=project,
            debug=debug,
            stability_window=stability_window,
        )
        logger.info("  Replication verified ✅")
        _log_sample_repositories(docker, cid)
    except ReplicationError as exc:
        result.error = str(exc)
        logger.error("Replication verification failed: %s", exc)
        _log_replication_failure(docker, cid)
        return result

    _log_final_stats(docker, cid, result, expected_count)
    logger.info("")
    logger.info("✅ Instance %s verification passed", slug)
    logger.info("")

    result.success = True
    return result
