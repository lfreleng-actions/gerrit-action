# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""HTTP and TCP health checks with configurable retries.

Replaces ``check-services.sh`` (416 lines) with a testable Python
implementation that provides:

- Container state verification
- Log polling for "Gerrit Code Review … ready" with timeout
- HTTP health checks with retries
- TCP port checks for replica/headless mode
- SSH keyscan verification
- Plugin verification via logs and HTTP API
- Replica mode detection

This module is the public entry point for the health-check tooling.
The individual pieces live in focused sibling modules and are
re-exported here, so callers continue to work with ``health_check``
alone:

* :mod:`health_check_model` — retry policy, log patterns, result record
* :mod:`health_check_container` — probes that read container state
* :mod:`health_check_network` — probes that talk HTTP, TCP and SSH
* :mod:`health_check_flows` — the retry loop and per-mode sequences

The per-instance orchestration stays here so that
:func:`check_instance` and :func:`check_all_instances` resolve every
step as an attribute of this module, which is how callers substitute
individual checks.

Usage::

    from docker_manager import DockerManager
    from health_check import (
        wait_for_gerrit_ready,
        http_health_check,
        tcp_port_check,
        verify_plugin_loaded,
        check_all_instances,
    )

    docker = DockerManager()
    wait_for_gerrit_ready(docker, container_id, timeout=180)
    http_health_check(url="http://10.0.0.2:8080/config/server/version")
    verify_plugin_loaded(docker, container_id, "pull-replication")
"""

from __future__ import annotations

# ``socket``, ``subprocess``, ``time`` and ``requests`` no longer have
# call sites in this module, but the checks that moved out reach those
# standard-library seams through ``health_check.<module>``; the
# attributes have to keep resolving here.
import logging
import socket  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
from typing import Any

import requests  # noqa: F401
from config import InstanceStore
from docker_manager import DockerManager
from errors import DockerError, HealthCheckError, PluginError
from health_check_container import (
    is_replica_mode,
    verify_container_running,
    verify_plugin_loaded,
    wait_for_gerrit_ready,
)
from health_check_flows import (
    _check_replica_health,
    _check_standard_health,
    wait_for_tcp_port,
)
from health_check_model import (
    _HEALTHY_HTTP_CODES,
    _READY_PATTERN,
    _REPLICA_PATTERN,
    GERRIT_READY_TIMEOUT,
    HTTP_RETRY,
    TCP_RETRY,
    TCP_SSH_RETRY,
    HealthCheckResult,
    RetryConfig,
)
from health_check_network import (
    http_health_check,
    tcp_port_check,
    verify_ssh_service,
)
from outputs import write_status_summary, write_summary

logger = logging.getLogger(__name__)

__all__ = [
    "GERRIT_READY_TIMEOUT",
    "HTTP_RETRY",
    "TCP_RETRY",
    "TCP_SSH_RETRY",
    "HealthCheckResult",
    "RetryConfig",
    # The underscore-prefixed entries are long-standing internals that
    # callers (notably the test suite) import from this module by name;
    # they are listed so the re-export stays explicit.
    "_HEALTHY_HTTP_CODES",
    "_READY_PATTERN",
    "_REPLICA_PATTERN",
    "_check_replica_health",
    "_check_standard_health",
    "check_all_instances",
    "check_instance",
    "http_health_check",
    "is_replica_mode",
    "tcp_port_check",
    "verify_container_running",
    "verify_plugin_loaded",
    "verify_ssh_service",
    "wait_for_gerrit_ready",
    "wait_for_tcp_port",
]


# ---------------------------------------------------------------------------
# Per-instance health check
# ---------------------------------------------------------------------------


def check_instance(
    docker: DockerManager,
    slug: str,
    instance: dict[str, Any],
    *,
    skip_plugin_install: bool = False,
    use_api_path: bool = False,
) -> HealthCheckResult:
    """Run all health checks for a single Gerrit instance.

    Parameters
    ----------
    docker:
        Docker CLI wrapper.
    slug:
        Instance slug.
    instance:
        Instance metadata dict (from ``instances.json``).
    skip_plugin_install:
        If *True*, skip plugin verification.
    use_api_path:
        If *True*, use the ``api_path`` from instance metadata.

    Returns
    -------
    HealthCheckResult
        The result of the health check.
    """
    result = HealthCheckResult(slug=slug)

    cid = instance.get("cid", "")
    container_ip = instance.get("ip", "")
    api_path = instance.get("api_path", "")

    # Compute effective API path (matching shell script logic)
    effective_api_path = ""
    if use_api_path and api_path:
        effective_api_path = api_path

    logger.info("========================================")
    logger.info("Checking instance: %s", slug)
    logger.info("========================================")
    logger.info("Container ID: %s", cid[:12] if cid else "(none)")
    logger.info("IP Address: %s", container_ip)
    logger.info("HTTP Port: %s (container port 8080)", instance.get("http_port", "?"))
    if api_path:
        logger.info(
            "API Path: %s (USE_API_PATH=%s)",
            api_path,
            "true" if use_api_path else "false",
        )
    logger.info("")

    try:
        # Step 1: Verify container is running
        verify_container_running(docker, cid, slug)
        logger.info("")

        # Step 2: Wait for Gerrit ready message
        wait_for_gerrit_ready(docker, cid)

        # Step 3: Check if replica mode
        result.is_replica = is_replica_mode(docker, cid)

        if result.is_replica:
            _check_replica_health(docker, cid, container_ip, slug)
        else:
            _check_standard_health(
                docker,
                cid,
                container_ip,
                slug,
                effective_api_path,
                skip_plugin_install=skip_plugin_install,
            )

        logger.info("")
        logger.info("✅ Instance %s is healthy and responding", slug)
        logger.info("")
        result.success = True

    except (HealthCheckError, DockerError, PluginError) as exc:
        result.error = str(exc)
        logger.error("Health check failed for %s: %s", slug, exc)

        # Try to grab container logs for diagnostics
        try:
            tail = docker.container_logs(cid, tail=50)
            logger.error("Container logs (last 50 lines):\n%s", tail)
        except DockerError as log_exc:
            logger.debug("Could not retrieve container logs for %s: %s", slug, log_exc)

    return result


# ---------------------------------------------------------------------------
# Multi-instance orchestrator
# ---------------------------------------------------------------------------


def check_all_instances(
    docker: DockerManager,
    instance_store: InstanceStore,
    *,
    skip_plugin_install: bool = False,
    use_api_path: bool = False,
) -> list[HealthCheckResult]:
    """Run health checks for all instances in the store.

    This is the top-level entry point that replaces the main loop in
    ``check-services.sh``.

    Parameters
    ----------
    docker:
        Docker CLI wrapper.
    instance_store:
        Loaded instance metadata.
    skip_plugin_install:
        Skip plugin verification if *True*.
    use_api_path:
        Use API path from instance metadata if *True*.

    Returns
    -------
    list[HealthCheckResult]
        Results for each instance, in slug order.

    Raises
    ------
    HealthCheckError
        If any instance failed its health check.
    """
    logger.info("Checking Gerrit service availability…")
    logger.info("")

    results: list[HealthCheckResult] = []

    for slug, instance in instance_store:
        r = check_instance(
            docker,
            slug,
            instance,
            skip_plugin_install=skip_plugin_install,
            use_api_path=use_api_path,
        )
        results.append(r)

    # Summary
    failed = [r for r in results if not r.success]

    logger.info("========================================")
    if not failed:
        logger.info("All service checks passed! ✅")
        logger.info("========================================")
        logger.info("")

        write_status_summary(
            "Service Health Checks",
            "All Gerrit instances are healthy and responding!",
            emoji="💚",
        )
    else:
        logger.error("Some service checks failed ❌")
        logger.info("========================================")
        logger.info("")

        lines = [
            "**Service Health Checks** ❌",
            "",
            "Some instances failed health checks.",
            "See logs above for details.",
            "",
        ]
        write_summary("\n".join(lines))

    # Show container status
    logger.info("Current container status:")
    try:
        ps_output = docker.ps(filter_name="gerrit-")
        if ps_output:
            logger.info("%s", ps_output)
    except DockerError as exc:
        logger.debug("Could not query container status: %s", exc)
    logger.info("")

    if failed:
        slugs = ", ".join(r.slug for r in failed)
        raise HealthCheckError(
            f"Health checks failed for: {slugs}",
            attempts=len(results),
        )

    return results
