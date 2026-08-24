# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Replication trigger flow for a single instance.

Split out of :mod:`replication`, which re-exports
:func:`trigger_replication` and keeps the multi-instance orchestration
that drives it.

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
import time
from typing import Any

import replication
from config import ActionConfig
from docker_manager import DockerManager
from errors import DockerError
from replication_model import TriggerResult
from replication_patterns import _MIN_WAIT_SECONDS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trigger steps
# ---------------------------------------------------------------------------


def _log_trigger_banner(
    cid: str,
    slug: str,
    gerrit_host: str,
    project: str,
    expected_count: int,
) -> None:
    """Announce which instance is about to be triggered."""
    logger.info("========================================")
    logger.info("Triggering replication: %s", slug)
    logger.info("========================================")
    logger.info("Container ID: %s", cid[:12] if cid else "(none)")
    logger.info("Source: %s", gerrit_host)
    if project:
        logger.info("Project filter: %s", project)
    if expected_count > 0:
        logger.info("Expected repositories: %d", expected_count)
    logger.info("")


def _verify_trigger_plugin(
    docker: DockerManager,
    cid: str,
    result: TriggerResult,
) -> bool:
    """Confirm the pull-replication plugin is present.

    Returns *False* (with ``result.error`` set) only when neither the
    plugin nor its jar file can be found; a jar that has not finished
    loading is accepted.
    """
    logger.info("Verifying pull-replication plugin is loaded…")
    if replication.verify_plugin_loaded(docker, cid, "pull-replication"):
        logger.info("Pull-replication plugin is active ✅")
        # Show plugin version from logs
        try:
            logs = docker.container_logs(cid, tail=200)
            for line in logs.splitlines():
                if "Loaded plugin pull-replication" in line:
                    logger.info("  %s", line.strip())
                    break
        except DockerError as exc:
            logger.debug("Could not read plugin load logs: %s", exc)
    else:
        # Check if jar file exists
        if docker.exec_test(cid, "-f /var/gerrit/plugins/pull-replication.jar"):
            logger.info("  Plugin file exists, may still be loading…")
        else:
            logger.warning("Plugin file not found in container")
            result.error = "pull-replication plugin not found"
            return False
    logger.info("")
    return True


def _log_replication_config(docker: DockerManager, cid: str) -> None:
    """Print the effective ``replication.config`` for the instance."""
    logger.info("Replication configuration:")
    config_content = replication.show_replication_config(docker, cid)
    if config_content:
        logger.info("--- replication.config ---")
        for line in config_content.splitlines():
            logger.info("  %s", line)
        logger.info("---")
    else:
        logger.warning("replication.config not found or empty")
    logger.info("")


def _ssh_trigger(docker: DockerManager, cid: str) -> None:
    """Ask Gerrit to start replication now, over SSH.

    Optional: a new installation has no admin SSH key yet, so failure
    here just means the fetchEvery schedule does the work instead.
    """
    logger.info("Attempting to trigger replication via SSH…")
    try:
        ssh_result = docker.exec_cmd(
            cid,
            "ssh -p 29418 -o StrictHostKeyChecking=no admin@localhost "
            "gerrit pull-replication start --wait --all 2>&1",
            timeout=30,
            check=False,
        )
        if any(
            err in ssh_result
            for err in ("ssh_failed", "Connection refused", "Permission denied")
        ):
            logger.warning("SSH trigger not available (expected for new installations)")
            logger.info("Replication will occur based on configured schedule")
        else:
            logger.info("SSH trigger response: %s", ssh_result)
            logger.info("✅ Replication triggered via SSH")
    except DockerError:
        logger.warning("SSH trigger failed, relying on fetchEvery polling")


def _await_replication_activity(
    docker: DockerManager,
    cid: str,
    config: ActionConfig,
) -> bool:
    """Poll the pull-replication log until it shows activity.

    Returns *True* once the log has any content, whether or not a
    completion line appeared before the wait budget ran out.
    """
    logger.info("")
    logger.info("Waiting for fetchEvery polling to trigger replication…")
    logger.info(
        "(First poll occurs within the configured fetch interval: %s)",
        config.fetch_every,
    )

    # Calculate wait timeout: 1.5× fetch interval, minimum 60s
    fetch_seconds = config.fetch_interval_seconds
    max_wait = max(fetch_seconds * 3 // 2, _MIN_WAIT_SECONDS)
    logger.info("Wait timeout: %ds (1.5× fetch interval)", max_wait)

    waited = 0
    replication_started = False

    while waited < max_wait:
        # Check pull_replication_log for activity
        if docker.exec_test(cid, "-f /var/gerrit/logs/pull_replication_log"):
            try:
                log_content = docker.exec_cmd(
                    cid,
                    "tail -n 50 /var/gerrit/logs/pull_replication_log 2>/dev/null",
                    check=False,
                    timeout=10,
                )
                if log_content.strip():
                    replication_started = True
                    if "completed" in log_content:
                        logger.info("✅ Replication activity detected and completed")
                        break
            except DockerError as exc:
                logger.debug("Could not read pull_replication_log: %s", exc)

        time.sleep(5)
        waited += 5
        if waited % 15 == 0:
            logger.info("  Still waiting… %ds elapsed", waited)

    if not replication_started and waited >= max_wait:
        logger.warning(
            "No replication activity detected after %ds. "
            "This may be normal if the fetch interval is longer. "
            "Replication will continue in background via fetchEvery polling.",
            max_wait,
        )

    return replication_started


def _log_replication_activity(docker: DockerManager, cid: str) -> None:
    """Echo the replication log tail and the matching container lines."""
    logger.info("")
    logger.info("Pull replication log (last 20 lines):")
    log_tail = replication.show_pull_replication_log(docker, cid, lines=20)
    for line in log_tail.splitlines():
        logger.info("  %s", line)
    logger.info("")

    logger.info("Container log replication activity:")
    try:
        logs = docker.container_logs(cid, tail=5000)
        repl_lines = [
            line
            for line in logs.splitlines()
            if re.search(r"pull-replication|fetch|FetchAll", line, re.IGNORECASE)
        ]
        for line in repl_lines[-10:]:
            logger.info("  %s", line.strip())
        if not repl_lines:
            logger.info("  (none)")
    except DockerError:
        logger.info("  (could not read logs)")
    logger.info("")


def _log_repository_listing(
    docker: DockerManager,
    cid: str,
    debug: bool,
) -> None:
    """List the replicated repositories, in full when debugging."""
    if debug:
        logger.info("(DEBUG=true: showing full repository list)")
        repos = replication.list_repositories(docker, cid, max_items=9999)
    else:
        logger.info("(showing first 50 repositories; set DEBUG=true for full list)")
        repos = replication.list_repositories(docker, cid, max_items=50)
    for line in repos.splitlines():
        logger.info("  %s", line)
    logger.info("")


def _report_trigger_outcome(
    result: TriggerResult,
    expected_count: int,
    replication_started: bool,
    config: ActionConfig,
) -> None:
    """Compare what was replicated against what the remote advertised."""
    if expected_count > 0:
        logger.info("Expected repositories: %d", expected_count)
        if result.repo_count >= expected_count and replication_started:
            logger.info(
                "✅ Replication complete: %d/%d repositories (log indicates activity)",
                result.repo_count,
                expected_count,
            )
        elif result.repo_count >= expected_count:
            logger.info(
                "⏳ Repo count matches but awaiting replication log confirmation: %d/%d",
                result.repo_count,
                expected_count,
            )
        elif result.repo_count > 2:
            logger.info(
                "⏳ Replication in progress: %d/%d repositories",
                result.repo_count,
                expected_count,
            )
        else:
            logger.warning("Replication may still be starting")
    elif result.repo_count > 0 and replication_started:
        logger.info(
            "✅ Replication appears to be working (%d repositories, log indicates activity)",
            result.repo_count,
        )
    elif result.repo_count > 0:
        logger.info(
            "⏳ Repositories found (%d) but awaiting replication log confirmation",
            result.repo_count,
        )
    elif config.sync_on_startup:
        logger.warning(
            "No replicated repositories detected. Replication may still be in progress."
        )


# ---------------------------------------------------------------------------
# Trigger replication
# ---------------------------------------------------------------------------


def trigger_replication(
    docker: DockerManager,
    cid: str,
    slug: str,
    instance: dict[str, Any],
    config: ActionConfig,
) -> TriggerResult:
    """Trigger initial replication for a single instance.

    This replaces the per-instance loop in ``trigger-replication.sh``.

    Steps:
    1. Verify replication.config exists
    2. Verify pull-replication plugin is loaded
    3. Show replication configuration
    4. Optionally trigger via SSH
    5. Wait for fetchEvery polling to show activity
    """
    result = TriggerResult(slug=slug)

    gerrit_host = instance.get("gerrit_host", "")
    project = instance.get("project", "")
    expected_count = int(instance.get("expected_project_count", 0))
    result.expected_count = expected_count

    _log_trigger_banner(cid, slug, gerrit_host, project, expected_count)

    if not replication.check_replication_config(docker, cid):
        logger.warning("replication.config not found, skipping replication trigger")
        result.error = "replication.config not found"
        return result

    if not config.skip_plugin_install and not _verify_trigger_plugin(
        docker, cid, result
    ):
        return result

    _log_replication_config(docker, cid)

    if config.auth_type == "ssh":
        _ssh_trigger(docker, cid)

    replication_started = _await_replication_activity(docker, cid, config)
    result.replication_started = replication_started

    _log_replication_activity(docker, cid)

    result.repo_count = replication.count_repositories(docker, cid)
    logger.info("Replicated repositories: %d", result.repo_count)
    _log_repository_listing(docker, cid, config.debug)

    _report_trigger_outcome(result, expected_count, replication_started, config)

    logger.info("")
    logger.info("Replication trigger completed for %s", slug)
    logger.info("")

    result.success = True
    return result
