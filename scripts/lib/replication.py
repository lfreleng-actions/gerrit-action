# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Replication trigger and verification for Gerrit pull-replication.

Replaces ``trigger-replication.sh`` (353 lines) and
``verify-replication.sh`` (740 lines) with a testable Python
implementation.

The pull-replication plugin is configured with ``fetchEvery`` which
polls the source Gerrit at regular intervals to fetch new/changed refs.

This module provides:

- Plugin and configuration verification
- SSH-based replication trigger (optional, for faster initial sync)
- Polling-based wait for replication activity
- Repository count and disk usage verification
- Detailed progress reporting

This module is the public entry point for the replication tooling.
The individual pieces live in focused sibling modules and are
re-exported here, so callers continue to work with ``replication``
alone:

* :mod:`replication_patterns` — tuning constants, log patterns and the
  shell commands run inside the container
* :mod:`replication_model` — trigger / verification results, the
  progress snapshot and the records the wait loop carries
* :mod:`replication_report` — the structured error scan result
* :mod:`replication_probe` — probes that read the replication config
  and scan for errors
* :mod:`replication_stats` — probes that count repositories, measure
  disk usage and read the replication log
* :mod:`replication_trigger` — the per-instance trigger flow
* :mod:`replication_wait` — the poll loop that decides when
  replication has finished
* :mod:`replication_diagnostics` — everything the wait loop prints,
  and its two failure paths
* :mod:`replication_verify` — the per-instance verification flow

The multi-instance orchestration stays here so that
:func:`trigger_all_instances` and :func:`verify_all_instances` resolve
every step as an attribute of this module, which is how callers
substitute individual stages.  :func:`take_snapshot` stays for the
same reason: it composes four probes that the test suite replaces
individually.

Usage::

    from docker_manager import DockerManager
    from replication import (
        trigger_replication,
        verify_replication,
        check_all_instances_replication,
    )

    docker = DockerManager()
    trigger_replication(docker, container_id, config)
    verify_replication(docker, container_id, slug, timeout=180)
"""

from __future__ import annotations

import logging
import time

from config import ActionConfig, InstanceStore
from docker_manager import DockerManager
from errors import DockerError, ReplicationError
from health_check import verify_plugin_loaded
from outputs import write_summary
from replication_model import (
    ReplicationSnapshot,
    TriggerResult,
    VerificationResult,
    _parse_int,
    _SeenMatches,
    _StabilityTracker,
    _WaitSettings,
)
from replication_patterns import (
    _COMPLETED_COUNT_CMD,
    _CONTAINER_ERROR_PATTERNS,
    _CONTINUATION_LINE_RE,
    _COUNT_REPOS_CMD,
    _DISK_USAGE_CMD,
    _DISK_USAGE_HUMAN_CMD,
    _MAGIC_REPO_NAMES,
    _MAGIC_REPO_RE,
    _MIN_KB_PER_REPO,
    _MIN_WAIT_SECONDS,
    _REPLICATION_ERROR_PATTERNS,
    _SOFT_FAILURE_PATTERNS,
    _STABILITY_WINDOW_SECONDS,
)
from replication_probe import (
    check_replication_config,
    check_replication_errors,
    check_secure_config,
    show_replication_config,
)
from replication_report import ErrorMatch, ReplicationErrorReport
from replication_stats import (
    check_pull_replication_log,
    check_replication_has_content,
    count_repositories,
    get_completed_repo_count,
    get_disk_usage_kb,
    get_git_disk_usage_human,
    get_git_disk_usage_mb,
    get_log_line_count,
    list_repositories,
    show_pull_replication_log,
)
from replication_trigger import trigger_replication
from replication_verify import verify_single_instance
from replication_wait import wait_for_replication

logger = logging.getLogger(__name__)

__all__ = [
    "DockerError",
    "ErrorMatch",
    "ReplicationErrorReport",
    "ReplicationSnapshot",
    "TriggerResult",
    "VerificationResult",
    # The underscore-prefixed entries are long-standing internals that
    # callers (notably the test suite) import from this module by name;
    # they are listed so the re-export stays explicit.
    "_COMPLETED_COUNT_CMD",
    "_CONTAINER_ERROR_PATTERNS",
    "_CONTINUATION_LINE_RE",
    "_COUNT_REPOS_CMD",
    "_DISK_USAGE_CMD",
    "_DISK_USAGE_HUMAN_CMD",
    "_MAGIC_REPO_NAMES",
    "_MAGIC_REPO_RE",
    "_MIN_KB_PER_REPO",
    "_MIN_WAIT_SECONDS",
    "_REPLICATION_ERROR_PATTERNS",
    "_SOFT_FAILURE_PATTERNS",
    "_STABILITY_WINDOW_SECONDS",
    "_SeenMatches",
    "_StabilityTracker",
    "_WaitSettings",
    "_parse_int",
    "check_pull_replication_log",
    "check_replication_config",
    "check_replication_errors",
    "check_replication_has_content",
    "check_secure_config",
    "count_repositories",
    "get_completed_repo_count",
    "get_disk_usage_kb",
    "get_git_disk_usage_human",
    "get_git_disk_usage_mb",
    "get_log_line_count",
    "list_repositories",
    "show_pull_replication_log",
    "show_replication_config",
    "take_snapshot",
    "trigger_all_instances",
    "trigger_replication",
    "verify_all_instances",
    "verify_plugin_loaded",
    "verify_single_instance",
    "wait_for_replication",
    "write_summary",
]


def take_snapshot(docker: DockerManager, cid: str) -> ReplicationSnapshot:
    """Capture a point-in-time snapshot of all replication indicators."""
    return ReplicationSnapshot(
        timestamp=time.time(),
        completed_count=get_completed_repo_count(docker, cid),
        disk_usage_kb=get_disk_usage_kb(docker, cid),
        log_line_count=get_log_line_count(docker, cid),
        repo_count=count_repositories(docker, cid),
    )


# ---------------------------------------------------------------------------
# Multi-instance orchestrators
# ---------------------------------------------------------------------------


def trigger_all_instances(
    docker: DockerManager,
    instance_store: InstanceStore,
    config: ActionConfig,
) -> list[TriggerResult]:
    """Trigger replication for all instances.

    This is the top-level entry point replacing ``trigger-replication.sh``.
    """
    logger.info("Triggering initial replication…")
    logger.info("")

    fetch_seconds = config.fetch_interval_seconds
    logger.info("Fetch interval: %s (%d seconds)", config.fetch_every, fetch_seconds)
    max_wait = max(fetch_seconds * 3 // 2, _MIN_WAIT_SECONDS)
    logger.info("Wait timeout: %ds (1.5× fetch interval)", max_wait)
    logger.info("")

    results: list[TriggerResult] = []

    for slug, instance in instance_store:
        cid = instance.get("cid", "")
        result = trigger_replication(docker, cid, slug, instance, config)
        results.append(result)

    # Summary
    failed = [r for r in results if not r.success]

    logger.info("========================================")
    if not failed:
        logger.info("Replication triggered for all instances ✅")
        logger.info("========================================")
        logger.info("")

        lines = [
            "**Replication Status** 🔄",
            "",
            "Replication has been triggered for all instances.",
            "",
            "_Note: Initial replication may take several minutes "
            "depending on repository sizes._",
            "",
        ]
        write_summary("\n".join(lines))
    else:
        logger.warning("Some replication triggers failed ⚠️")
        logger.info("========================================")
        logger.info("")

        lines = [
            "**Replication Trigger Status** ⚠️",
            "",
            "Some replication triggers encountered issues.",
            "Check logs for details.",
            "",
        ]
        write_summary("\n".join(lines))

    # Monitoring instructions
    monitor_lines = [
        "To monitor ongoing replication, check container logs:",
        "```bash",
    ]
    for _slug, instance in instance_store:
        cid = instance.get("cid", "")
        monitor_lines.append(f"docker logs -f {cid} | grep replication")
    monitor_lines.extend(["```", ""])
    write_summary("\n".join(monitor_lines))

    return results


def verify_all_instances(
    docker: DockerManager,
    instance_store: InstanceStore,
    timeout: int = 180,
    debug: bool = False,
    stability_window: int = _STABILITY_WINDOW_SECONDS,
) -> list[VerificationResult]:
    """Verify replication for all instances.

    This is the top-level entry point replacing ``verify-replication.sh``.

    Raises :class:`ReplicationError` if any instance fails.
    """
    logger.info("Verifying replication success…")
    logger.info("")

    results: list[VerificationResult] = []
    total = 0

    for slug, instance in instance_store:
        total += 1
        r = verify_single_instance(
            docker,
            slug,
            instance,
            timeout=timeout,
            debug=debug,
            stability_window=stability_window,
        )
        results.append(r)

    # Summary
    failed = [r for r in results if not r.success]

    logger.info("========================================")
    logger.info("Verification Summary")
    logger.info("========================================")
    logger.info("Total instances: %d", total)
    logger.info("Failed: %d", len(failed))
    logger.info("")

    if not failed:
        logger.info("All replication verifications passed! ✅")
        logger.info("")

        # Disk usage summary
        logger.info("========================================")
        logger.info("Disk Usage Summary")
        logger.info("========================================")
        for r in results:
            logger.info("")
            logger.info("Instance: %s", r.slug)
            logger.info("  Disk usage: %s", r.disk_usage)
        logger.info("")

        # Step summary
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
        write_summary("\n".join(summary_lines))
    else:
        logger.error("Some verifications failed ❌")
        logger.info("")

        lines = [
            "**Replication Verification** ❌",
            "",
            f"{len(failed)} of {total} instances failed verification.",
            "",
            "Check the workflow logs for detailed error information.",
            "",
        ]
        write_summary("\n".join(lines))

        slugs = ", ".join(r.slug for r in failed)
        raise ReplicationError(
            f"Replication verification failed for: {slugs}",
            expected_count=sum(r.expected_count for r in failed),
            actual_count=sum(r.repo_count for r in failed),
        )

    return results
