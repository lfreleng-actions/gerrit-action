# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Retry loop and per-mode health-check sequences.

These three helpers sit between the individual probes in
:mod:`health_check_container` / :mod:`health_check_network` and the
per-instance orchestration in :mod:`health_check`.

They reach their collaborators through the :mod:`health_check` facade
rather than importing them directly.  Every probe in this package is
re-exported there, and callers (the test suite in particular) rebind
those attributes to stub out a step; resolving the name on the facade
at call time is what keeps that substitution working now the probes
live in sibling modules.  :mod:`health_check` imports this module for
its own re-exports, so the import below is deliberately circular; only
attribute lookups happen at call time, never at import time.
"""

from __future__ import annotations

import logging
import time

import health_check
from docker_manager import DockerManager
from errors import HealthCheckError
from health_check_model import (
    HTTP_RETRY,
    TCP_RETRY,
    TCP_SSH_RETRY,
    RetryConfig,
)

logger = logging.getLogger(__name__)


def wait_for_tcp_port(
    host: str,
    port: int,
    retry: RetryConfig = TCP_RETRY,
    label: str = "",
) -> bool:
    """Wait for a TCP port to become available.

    Parameters
    ----------
    host:
        Hostname or IP address.
    port:
        Port number.
    retry:
        Retry configuration.
    label:
        Human-readable label for log messages (e.g. "HTTP port 8080").

    Returns
    -------
    bool
        *True* if the port became available.

    Raises
    ------
    HealthCheckError
        If the port did not become available within the allowed retries.
    """
    display = label or f"{host}:{port}"
    logger.info("Waiting for TCP port: %s", display)

    for attempt in range(1, retry.max_retries + 1):
        if health_check.tcp_port_check(host, port):
            logger.info("  TCP port %s is listening ✅", display)
            return True

        if attempt < retry.max_retries:
            time.sleep(retry.interval)
            if attempt % 5 == 0:
                logger.info(
                    "  Retry %d/%d (waiting for %s)",
                    attempt,
                    retry.max_retries,
                    display,
                )

    raise HealthCheckError(
        f"TCP port {display} not listening after {retry.max_retries} retries",
        url=display,
        attempts=retry.max_retries,
    )


# ---------------------------------------------------------------------------
# Replica-mode health check flow
# ---------------------------------------------------------------------------


def _check_replica_health(
    _docker: DockerManager,
    _cid: str,
    container_ip: str,
    _slug: str,
) -> bool:
    """Run health checks for a replica/headless Gerrit instance.

    In replica mode the REST API is disabled, so we check:
    1. HTTP port (8080) is listening via TCP
    2. SSH port (29418) is listening via TCP
    3. SSH service responds with a host key

    Returns *True* on success, raises :class:`HealthCheckError` on failure.
    """
    logger.info("Gerrit is running in replica/headless mode")
    logger.info("Using TCP port checks (REST API is disabled in this mode)…")

    # Step 1: HTTP port
    logger.info("")
    logger.info("Step 1: Checking HTTP port (8080)…")
    health_check.wait_for_tcp_port(
        container_ip,
        8080,
        retry=TCP_RETRY,
        label="HTTP port 8080",
    )

    # Step 2: SSH port
    logger.info("")
    logger.info("Step 2: Checking SSH port (29418)…")
    health_check.wait_for_tcp_port(
        container_ip,
        29418,
        retry=TCP_SSH_RETRY,
        label="SSH port 29418",
    )

    # Step 3: SSH service verification
    logger.info("")
    logger.info("Step 3: Verifying Gerrit SSH service…")
    health_check.verify_ssh_service(container_ip, port=29418)

    logger.info("")
    logger.info("Replica mode health checks passed ✅")
    return True


# ---------------------------------------------------------------------------
# Standard (non-replica) health check flow
# ---------------------------------------------------------------------------


def _check_standard_health(
    docker: DockerManager,
    cid: str,
    container_ip: str,
    _slug: str,
    effective_api_path: str,
    skip_plugin_install: bool = False,
) -> bool:
    """Run health checks for a standard (non-replica) Gerrit instance.

    1. HTTP health check on the version endpoint.
    2. Plugin verification (pull-replication, replication-api).

    Returns *True* on success, raises :class:`HealthCheckError` on failure.
    """
    logger.info("Performing HTTP health check…")

    # Build health check URL
    if effective_api_path:
        health_url = (
            f"http://{container_ip}:8080{effective_api_path}/config/server/version"
        )
    else:
        health_url = f"http://{container_ip}:8080/config/server/version"

    logger.info("Health check URL: %s", health_url)
    health_check.http_health_check(health_url, retry=HTTP_RETRY)

    # Plugin checks
    if not skip_plugin_install:
        logger.info("")
        logger.info("Verifying pull-replication plugin…")
        health_check.verify_plugin_loaded(
            docker,
            cid,
            "pull-replication",
            container_ip=container_ip,
            effective_api_path=effective_api_path,
        )

        # Also check replication-api (dependency)
        if docker.grep_logs(cid, "Loaded plugin replication-api", tail=1000):
            logger.info("Replication-api plugin loaded ✅")

    return True
