# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Helpers for the replication *trigger* phase.

``trigger_replication`` and ``trigger_all_instances`` in
:mod:`replication` orchestrate a fixed sequence of steps; this module
owns the parts of that sequence that are self-contained — banner
logging, the optional SSH kick, the short "has the first fetchEvery
poll landed yet?" wait, and the outcome / step-summary wording.

Keeping them here lets the orchestrator read as a list of steps while
the wording and the shell details stay reviewable in one place.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import replication_diagnostics as diagnostics
from config import ActionConfig
from docker_manager import DockerManager
from errors import DockerError
from replication_state import TriggerResult

logger = logging.getLogger(__name__)

# Minimum wait time for replication regardless of fetch interval
_MIN_WAIT_SECONDS = 60


def fetch_wait_seconds(config: ActionConfig) -> int:
    """Return the wait budget for one fetchEvery cycle.

    1.5× the configured fetch interval, floored at
    :data:`_MIN_WAIT_SECONDS` so a very short interval still leaves
    the plugin time to complete its first poll.
    """
    return max(config.fetch_interval_seconds * 3 // 2, _MIN_WAIT_SECONDS)


def log_trigger_header(
    slug: str,
    cid: str,
    instance: dict[str, Any],
    expected_count: int,
) -> None:
    """Log the banner introducing a per-instance replication trigger."""
    gerrit_host = instance.get("gerrit_host", "")
    project = instance.get("project", "")

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


def log_plugin_version(docker: DockerManager, cid: str) -> None:
    """Log the pull-replication plugin's load line from the container."""
    try:
        logs = docker.container_logs(cid, tail=200)
        for line in logs.splitlines():
            if "Loaded plugin pull-replication" in line:
                logger.info("  %s", line.strip())
                break
    except DockerError as exc:
        logger.debug("Could not read plugin load logs: %s", exc)


def log_trigger_run_header(config: ActionConfig) -> None:
    """Log the fetch-interval banner shown once per trigger run."""
    logger.info("Triggering initial replication…")
    logger.info("")
    logger.info(
        "Fetch interval: %s (%d seconds)",
        config.fetch_every,
        config.fetch_interval_seconds,
    )
    logger.info("Wait timeout: %ds (1.5× fetch interval)", fetch_wait_seconds(config))
    logger.info("")


def plugin_state_error(docker: DockerManager, cid: str, *, loaded: bool) -> str:
    """Report the pull-replication plugin state, or why it is unusable.

    *loaded* is the outcome of the plugin health check, taken as an
    argument so that probe keeps running at its original call site in
    the orchestrator.  The on-disk jar is only inspected when the
    health check came back negative, preserving the original
    short-circuit: a loaded plugin never triggers a container exec.

    Returns an error string when the plugin is genuinely absent, or
    "" when it is loaded (or merely still loading).
    """
    if loaded:
        logger.info("Pull-replication plugin is active ✅")
        log_plugin_version(docker, cid)
    elif docker.exec_test(cid, "-f /var/gerrit/plugins/pull-replication.jar"):
        logger.info("  Plugin file exists, may still be loading…")
    else:
        logger.warning("Plugin file not found in container")
        return "pull-replication plugin not found"
    return ""


def log_replication_config(config_content: str) -> None:
    """Echo the effective ``replication.config`` into the workflow log."""
    logger.info("Replication configuration:")
    if config_content:
        logger.info("--- replication.config ---")
        for line in config_content.splitlines():
            logger.info("  %s", line)
        logger.info("---")
    else:
        logger.warning("replication.config not found or empty")
    logger.info("")


def ssh_trigger(docker: DockerManager, cid: str) -> None:
    """Ask Gerrit to start replication now, over the SSH admin port.

    Purely an optimisation for the initial sync: a fresh installation
    usually has no admin SSH key yet, so every failure mode here is
    downgraded to a warning and replication is left to the fetchEvery
    schedule.
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


def wait_for_initial_activity(
    docker: DockerManager,
    cid: str,
    config: ActionConfig,
) -> bool:
    """Poll until the pull-replication log shows the first fetch.

    Returns *True* once any content appears in the per-event log.
    A timeout is **not** an error: with a long fetch interval the
    first poll may legitimately land after the wait budget, and the
    plugin keeps polling in the background either way.
    """
    logger.info("")
    logger.info("Waiting for fetchEvery polling to trigger replication…")
    logger.info(
        "(First poll occurs within the configured fetch interval: %s)",
        config.fetch_every,
    )

    max_wait = fetch_wait_seconds(config)
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


def log_container_replication_activity(docker: DockerManager, cid: str) -> None:
    """Log the last few replication-related container log lines."""
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


def log_replication_log_tail(log_tail: str) -> None:
    """Echo the tail of the pull-replication log after a trigger."""
    logger.info("")
    logger.info("Pull replication log (last 20 lines):")
    diagnostics.log_indented_info_lines(log_tail)
    logger.info("")


def log_repository_listing_intro(repo_count: int, debug: bool) -> int:
    """Announce the replicated-repository listing and size it.

    Returns the ``max_items`` cap the caller should pass to
    ``list_repositories``: effectively unbounded in debug mode,
    otherwise the first 50 entries.
    """
    logger.info("Replicated repositories: %d", repo_count)
    if debug:
        logger.info("(DEBUG=true: showing full repository list)")
        return 9999
    logger.info("(showing first 50 repositories; set DEBUG=true for full list)")
    return 50


def log_trigger_outcome(result: TriggerResult, sync_on_startup: bool) -> None:
    """Summarise how far replication got, without judging it a failure.

    The trigger step never fails the workflow on counts alone — the
    authoritative pass/fail decision belongs to the verification
    step — so every branch here is informational.
    """
    expected_count = result.expected_count
    if expected_count > 0:
        logger.info("Expected repositories: %d", expected_count)
        if result.repo_count >= expected_count and result.replication_started:
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
    elif result.repo_count > 0 and result.replication_started:
        logger.info(
            "✅ Replication appears to be working (%d repositories, log indicates activity)",
            result.repo_count,
        )
    elif result.repo_count > 0:
        logger.info(
            "⏳ Repositories found (%d) but awaiting replication log confirmation",
            result.repo_count,
        )
    elif sync_on_startup:
        logger.warning(
            "No replicated repositories detected. Replication may still be in progress."
        )


def log_trigger_completed(slug: str) -> None:
    """Log the closing lines of a per-instance replication trigger."""
    logger.info("")
    logger.info("Replication trigger completed for %s", slug)
    logger.info("")


def trigger_run_summary(*, any_failed: bool) -> str:
    """Log the cross-instance banner and return its step-summary markdown."""
    logger.info("========================================")
    if not any_failed:
        logger.info("Replication triggered for all instances ✅")
        logger.info("========================================")
        logger.info("")
        return trigger_success_summary()
    logger.warning("Some replication triggers failed ⚠️")
    logger.info("========================================")
    logger.info("")
    return trigger_failure_summary()


def trigger_success_summary() -> str:
    """Step-summary markdown for "every trigger succeeded"."""
    lines = [
        "**Replication Status** 🔄",
        "",
        "Replication has been triggered for all instances.",
        "",
        "_Note: Initial replication may take several minutes "
        "depending on repository sizes._",
        "",
    ]
    return "\n".join(lines)


def trigger_failure_summary() -> str:
    """Step-summary markdown for "at least one trigger had issues"."""
    lines = [
        "**Replication Trigger Status** ⚠️",
        "",
        "Some replication triggers encountered issues.",
        "Check logs for details.",
        "",
    ]
    return "\n".join(lines)


def monitor_instructions_summary(cids: list[str]) -> str:
    """Step-summary markdown showing how to follow replication live."""
    monitor_lines = [
        "To monitor ongoing replication, check container logs:",
        "```bash",
    ]
    for cid in cids:
        monitor_lines.append(f"docker logs -f {cid} | grep replication")
    monitor_lines.extend(["```", ""])
    return "\n".join(monitor_lines)
