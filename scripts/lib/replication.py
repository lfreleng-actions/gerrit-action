# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Replication trigger and verification for Gerrit pull-replication.

Replaces ``trigger-replication.sh`` (353 lines) and
``verify-replication.sh`` (740 lines) with a testable Python
implementation.

The pull-replication plugin is configured with ``fetchEvery`` which
polls the source Gerrit at regular intervals to fetch new/changed refs.

This module holds the orchestration — the ordered sequence of steps
that make up a trigger or a verification run — and re-exports the
supporting pieces so ``from replication import ...`` keeps working:

- ``replication_state`` — result / snapshot value types
- ``replication_report`` — error-report data model
- ``replication_scan`` — error patterns and the log scanner
- ``replication_probes`` — read-only container queries
- ``replication_diagnostics`` — rendering of error reports
- ``replication_poll`` — the wait-for-completion loop
- ``replication_trigger`` / ``replication_wait`` /
  ``replication_verify`` — per-phase logging and decision helpers

Every container probe is called from this module rather than from the
phase helpers, so the probes stay bound to ``replication``'s namespace
and remain interceptable at a single, stable location.  The poll loop
is the one exception: it is handed the same bindings explicitly, as a
:class:`~replication_poll.PollProbes` bundle built by
:func:`_poll_probes`.

Usage::

    from docker_manager import DockerManager
    from replication import trigger_all_instances, verify_all_instances

    docker = DockerManager()
    trigger_all_instances(docker, instance_store, config)
    verify_all_instances(docker, instance_store, timeout=180)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import replication_diagnostics as diagnostics
import replication_poll as poll
import replication_trigger as trigger_steps
import replication_verify as verify_steps
from config import ActionConfig, InstanceStore
from docker_manager import DockerManager
from errors import ReplicationError
from health_check import verify_plugin_loaded
from outputs import write_summary
from replication_poll import PollProbes
from replication_probes import (
    _parse_int,
    check_pull_replication_log,
    check_replication_config,
    check_replication_has_content,
    check_secure_config,
    count_repositories,
    get_completed_repo_count,
    get_disk_usage_kb,
    get_git_disk_usage_human,
    get_git_disk_usage_mb,
    get_log_line_count,
    list_repositories,
    show_pull_replication_log,
    show_replication_config,
)
from replication_report import ErrorMatch, ReplicationErrorReport
from replication_scan import check_replication_errors
from replication_state import (
    _STABILITY_WINDOW_SECONDS,
    ReplicationSnapshot,
    TriggerResult,
    VerificationResult,
    _StabilityTracker,
)

logger = logging.getLogger(__name__)

# Names re-exported for callers and tests that import them from
# ``replication``.  The implementations moved into the sibling
# ``replication_*`` modules; this list is the compatibility surface.
__all__ = [
    "ErrorMatch",
    "ReplicationErrorReport",
    "ReplicationSnapshot",
    "TriggerResult",
    "VerificationResult",
    "_StabilityTracker",
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
    "verify_single_instance",
    "wait_for_replication",
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
    expected_count = int(instance.get("expected_project_count", 0))
    result.expected_count = expected_count
    trigger_steps.log_trigger_header(slug, cid, instance, expected_count)

    if not check_replication_config(docker, cid):
        logger.warning("replication.config not found, skipping replication trigger")
        result.error = "replication.config not found"
        return result

    if not config.skip_plugin_install:
        logger.info("Verifying pull-replication plugin is loaded…")
        result.error = trigger_steps.plugin_state_error(
            docker,
            cid,
            loaded=verify_plugin_loaded(docker, cid, "pull-replication"),
        )
        if result.error:
            return result
        logger.info("")

    trigger_steps.log_replication_config(show_replication_config(docker, cid))

    # SSH trigger (optional, for faster initial sync)
    if config.auth_type == "ssh":
        trigger_steps.ssh_trigger(docker, cid)

    result.replication_started = trigger_steps.wait_for_initial_activity(
        docker, cid, config
    )

    trigger_steps.log_replication_log_tail(
        show_pull_replication_log(docker, cid, lines=20)
    )
    trigger_steps.log_container_replication_activity(docker, cid)

    result.repo_count = count_repositories(docker, cid)
    max_items = trigger_steps.log_repository_listing_intro(
        result.repo_count, config.debug
    )
    diagnostics.log_indented_info_lines(
        list_repositories(docker, cid, max_items=max_items)
    )
    logger.info("")

    trigger_steps.log_trigger_outcome(result, config.sync_on_startup)
    trigger_steps.log_trigger_completed(slug)

    result.success = True
    return result


def _poll_probes() -> PollProbes:
    """Bind this module's container readers for :mod:`replication_poll`.

    Resolved on every call rather than once at import time, so the
    bundle always reflects the readers currently installed on this
    module.
    """
    return PollProbes(
        take_snapshot=take_snapshot,
        count_repositories=count_repositories,
        check_replication_errors=check_replication_errors,
        check_pull_replication_log=check_pull_replication_log,
        check_replication_has_content=check_replication_has_content,
        get_git_disk_usage_human=get_git_disk_usage_human,
        list_repositories=list_repositories,
        show_pull_replication_log=show_pull_replication_log,
    )


def wait_for_replication(
    docker: DockerManager,
    cid: str,
    slug: str,
    timeout: int,
    expected_count: int = 0,
    project: str = "",
    debug: bool = False,
    stability_window: int = _STABILITY_WINDOW_SECONDS,
) -> bool:
    """Wait for replication to complete for a single instance.

    Thin entry point over :func:`replication_poll.wait_for_replication`
    that supplies this module's container readers.  See that function
    for the completion signals and failure modes.

    Returns *True* on success.
    Raises :class:`ReplicationError` on timeout or persistent errors.
    """
    return poll.wait_for_replication(
        _poll_probes(),
        docker,
        cid,
        slug,
        timeout,
        expected_count=expected_count,
        project=project,
        debug=debug,
        stability_window=stability_window,
    )


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
    expected_count = int(instance.get("expected_project_count", 0))
    result.expected_count = expected_count
    verify_steps.log_verify_header(slug, cid, instance)

    result.error = verify_steps.container_state_error(docker, cid)
    if result.error:
        return result

    verify_steps.log_step("Step 1: Verifying pull-replication plugin…")
    if not verify_plugin_loaded(docker, cid, "pull-replication"):
        result.error = "Pull-replication plugin not loaded"
        logger.error("%s ❌", result.error)
        return result

    verify_steps.log_step("Step 2: Verifying replication configuration…")
    if not check_replication_config(docker, cid):
        result.error = "replication.config not found"
        logger.error("%s ❌", result.error)
        return result
    logger.info("  replication.config found ✅")
    verify_steps.log_config_content(show_replication_config(docker, cid))

    verify_steps.log_step("Step 2b: Verifying authentication configuration…")
    check_secure_config(docker, cid)

    # Failure gating is identical to ``wait_for_replication``'s step 1:
    # only authoritative user-project errors can fail verification.
    # Magic-repo failures (``All-Users`` etc.) and container-log
    # advisory signals surface as warnings so the operator sees them
    # without the action bailing on environmental ACL restrictions
    # or startup chatter.
    verify_steps.log_step("Step 3: Checking for replication errors…")
    error_report = check_replication_errors(docker, cid)
    diagnostics.log_non_fatal_matches(error_report)
    if error_report.has_user_project_errors:
        result.error = verify_steps.log_error_evidence(
            error_report, show_pull_replication_log(docker, cid, lines=500)
        )
        return result
    logger.info("  No replication errors detected ✅")

    verify_steps.log_step("Step 4: Waiting for replicated repositories…")
    try:
        wait_for_replication(
            docker,
            cid,
            slug,
            timeout=timeout,
            expected_count=expected_count,
            project=instance.get("project", ""),
            debug=debug,
            stability_window=stability_window,
        )
        logger.info("  Replication verified ✅")
        verify_steps.log_sample_repositories(docker, cid)
    except ReplicationError as exc:
        result.error = str(exc)
        logger.error("Replication verification failed: %s", exc)
        verify_steps.log_recent_replication_logs(docker, cid)
        return result

    verify_steps.log_step("Step 5: Final replication statistics…")
    result.repo_count = count_repositories(docker, cid)
    result.completed_count = get_completed_repo_count(docker, cid)
    result.disk_usage = get_git_disk_usage_human(docker, cid)
    result.disk_usage_mb = get_git_disk_usage_mb(docker, cid)
    verify_steps.log_final_stats(result)

    result.success = True
    return result


def trigger_all_instances(
    docker: DockerManager,
    instance_store: InstanceStore,
    config: ActionConfig,
) -> list[TriggerResult]:
    """Trigger replication for all instances.

    This is the top-level entry point replacing ``trigger-replication.sh``.
    """
    trigger_steps.log_trigger_run_header(config)

    results: list[TriggerResult] = []
    for slug, instance in instance_store:
        cid = instance.get("cid", "")
        results.append(trigger_replication(docker, cid, slug, instance, config))

    failed = [r for r in results if not r.success]
    write_summary(trigger_steps.trigger_run_summary(any_failed=bool(failed)))

    # Monitoring instructions
    cids = [instance.get("cid", "") for _slug, instance in instance_store]
    write_summary(trigger_steps.monitor_instructions_summary(cids))

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
    for slug, instance in instance_store:
        results.append(
            verify_single_instance(
                docker,
                slug,
                instance,
                timeout=timeout,
                debug=debug,
                stability_window=stability_window,
            )
        )

    failed = [r for r in results if not r.success]
    verify_steps.log_verification_banner(len(results), len(failed))

    if failed:
        logger.error("Some verifications failed ❌")
        logger.info("")
        write_summary(
            verify_steps.verification_failure_summary(len(failed), len(results))
        )
        raise verify_steps.verification_failure_error(failed)

    logger.info("All replication verifications passed! ✅")
    logger.info("")
    verify_steps.log_disk_usage_summary(results)
    write_summary(verify_steps.verification_success_summary(results))

    return results
