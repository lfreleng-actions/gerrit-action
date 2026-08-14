# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Operator-facing reporting for health-check runs.

The probes and flows in :mod:`health_check` decide *whether* an
instance is healthy; this module owns *how a run is presented* — the
banner printed before each instance, the periodic retry progress, the
diagnostics dumped when a check fails, the closing verdict, the
``docker ps`` listing, and the Markdown written to the GitHub step
summary.

Keeping presentation out of the flows lets those read as a plain
sequence of checks, and gives every console and step-summary string a
single place to live so wording can change without touching probe
logic.
"""

from __future__ import annotations

import logging
from typing import Any

from docker_manager import DockerManager
from errors import DockerError

logger = logging.getLogger(__name__)

# Step-summary wording for a run in which every instance passed.
HEALTH_SUMMARY_TITLE = "Service Health Checks"
HEALTH_SUMMARY_OK_BODY = "All Gerrit instances are healthy and responding!"
HEALTH_SUMMARY_OK_EMOJI = "💚"


def log_instance_banner(
    slug: str,
    instance: dict[str, Any],
    *,
    use_api_path: bool,
) -> None:
    """Log the header block that introduces one instance's checks.

    Args:
        slug: Instance slug.
        instance: Instance metadata dict (from ``instances.json``).
        use_api_path: Whether the ``USE_API_PATH`` policy is enabled.
    """
    cid = instance.get("cid", "")
    api_path = instance.get("api_path", "")

    logger.info("========================================")
    logger.info("Checking instance: %s", slug)
    logger.info("========================================")
    logger.info("Container ID: %s", cid[:12] if cid else "(none)")
    logger.info("IP Address: %s", instance.get("ip", ""))
    logger.info("HTTP Port: %s (container port 8080)", instance.get("http_port", "?"))
    if api_path:
        logger.info(
            "API Path: %s (USE_API_PATH=%s)",
            api_path,
            "true" if use_api_path else "false",
        )
    logger.info("")


def log_retry_progress(attempt: int, max_retries: int, detail: str) -> None:
    """Log a progress line on every fifth retry attempt.

    Health checks can retry dozens of times; reporting only every
    fifth attempt keeps the job log readable while still showing that
    the action is making progress.

    Args:
        attempt: 1-based attempt number that just failed.
        max_retries: Total attempts the caller will make.
        detail: Short description of what is being waited on.
    """
    if attempt % 5 == 0:
        logger.info("  Retry %d/%d (%s)", attempt, max_retries, detail)


def log_ssh_host_key(output: str, host: str, port: int) -> None:
    """Log the outcome of an ``ssh-keyscan`` against a Gerrit instance.

    Args:
        output: Raw keyscan output; empty if the service did not respond.
        host: Hostname or IP address that was scanned.
        port: SSH port that was scanned.
    """
    if not output:
        logger.warning(
            "Could not retrieve SSH host key from %s:%d. "
            "SSH port is open but service may not be fully ready.",
            host,
            port,
        )
        return

    # Show a truncated version of the first key for logging
    first_line = output.splitlines()[0]
    parts = first_line.split()
    key_preview = f"{parts[1]} {parts[2][:40]}…" if len(parts) >= 3 else first_line[:60]
    logger.info("  Gerrit SSH service is responding ✅")
    logger.info("  Host key received: %s", key_preview)


def log_failure_diagnostics(docker: DockerManager, cid: str, slug: str) -> None:
    """Dump recent container logs after a failed health check.

    Failures are best explained by whatever Gerrit last wrote, so the
    log tail is emitted at ``ERROR`` level alongside the failure.  A
    container that has already gone away must not mask the original
    error, so retrieval problems are only logged at ``DEBUG``.

    Args:
        docker: Docker CLI wrapper.
        cid: Container ID.
        slug: Instance slug for logging.
    """
    try:
        tail = docker.container_logs(cid, tail=50)
        logger.error("Container logs (last 50 lines):\n%s", tail)
    except DockerError as log_exc:
        logger.debug("Could not retrieve container logs for %s: %s", slug, log_exc)


def log_run_verdict(*, failed: bool) -> None:
    """Log the closing banner for a multi-instance health-check run."""
    logger.info("========================================")
    if failed:
        logger.error("Some service checks failed ❌")
    else:
        logger.info("All service checks passed! ✅")
    logger.info("========================================")
    logger.info("")


def log_container_status(docker: DockerManager) -> None:
    """Log the current ``docker ps`` listing for Gerrit containers."""
    logger.info("Current container status:")
    try:
        ps_output = docker.ps(filter_name="gerrit-")
        if ps_output:
            logger.info("%s", ps_output)
    except DockerError as exc:
        logger.debug("Could not query container status: %s", exc)
    logger.info("")


def failure_summary_markdown() -> str:
    """Return the step-summary Markdown for a run with failed instances."""
    lines = [
        "**Service Health Checks** ❌",
        "",
        "Some instances failed health checks.",
        "See logs above for details.",
        "",
    ]
    return "\n".join(lines)
