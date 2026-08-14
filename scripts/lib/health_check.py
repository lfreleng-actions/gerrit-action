# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Service-level health checks for running Gerrit instances.

Replaces ``check-services.sh`` (416 lines) with a testable Python
implementation.  This module owns the probes that talk to Gerrit over
the network — HTTP, TCP and SSH — plus the flows that sequence every
probe into a verdict for one instance and then for a whole set of
instances.

Supporting modules, whose public names are re-exported here:

- :mod:`health_check_retry` — retry and timeout budgets.
- :mod:`health_check_endpoints` — ``USE_API_PATH`` policy and URLs.
- :mod:`health_check_container` — Docker-level probes: container
  state, readiness log polling, replica detection, plugin loading.
- :mod:`health_check_report` — console and step-summary output.

Usage::

    from docker_manager import DockerManager
    from health_check import check_all_instances, http_health_check

    http_health_check(url="http://10.0.0.2:8080/config/server/version")
    check_all_instances(DockerManager(), instance_store)
"""

from __future__ import annotations

import logging
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import requests
from config import InstanceStore
from docker_manager import DockerManager
from errors import DockerError, HealthCheckError, PluginError
from health_check_container import (
    is_replica_mode,
    verify_container_running,
    verify_plugin_loaded,
    wait_for_gerrit_ready,
)
from health_check_endpoints import endpoint_url, resolve_api_path
from health_check_report import (
    HEALTH_SUMMARY_OK_BODY,
    HEALTH_SUMMARY_OK_EMOJI,
    HEALTH_SUMMARY_TITLE,
    failure_summary_markdown,
    log_container_status,
    log_failure_diagnostics,
    log_instance_banner,
    log_retry_progress,
    log_run_verdict,
    log_ssh_host_key,
)
from health_check_retry import HTTP_RETRY, TCP_RETRY, TCP_SSH_RETRY, RetryConfig
from outputs import write_status_summary, write_summary

logger = logging.getLogger(__name__)

# HTTP status codes considered "healthy" (the endpoint exists and responds)
_HEALTHY_HTTP_CODES = {200, 401, 403}


# --- HTTP probe -----------------------------------------------------------
def http_health_check(url: str, retry: RetryConfig = HTTP_RETRY) -> int:
    """Perform an HTTP health check with retries.

    The check succeeds when the endpoint responds with one of the
    "healthy" status codes (200, 401, 403).

    Args:
        url: Full URL to check, e.g.
            ``http://10.0.0.2:8080/config/server/version``.
        retry: Retry configuration.

    Returns:
        The HTTP status code that passed the check.

    Raises:
        HealthCheckError: If the endpoint did not return a healthy
            status code within the allowed retries.
    """
    logger.info("HTTP health check: %s", url)
    last_code: int | None = None

    for attempt in range(1, retry.max_retries + 1):
        try:
            resp = requests.get(url, timeout=(5, 10), allow_redirects=False)
            last_code = resp.status_code

            if last_code in _HEALTHY_HTTP_CODES:
                logger.info("HTTP check passed (code: %d) ✅", last_code)
                return last_code

        except requests.RequestException:
            last_code = None

        if attempt < retry.max_retries:
            time.sleep(retry.interval)
            detail = f"HTTP code: {last_code or 'N/A'}"
            log_retry_progress(attempt, retry.max_retries, detail)

    raise HealthCheckError(
        f"HTTP health check failed for {url} after {retry.max_retries} retries "
        f"(last HTTP code: {last_code})",
        url=url,
        last_status_code=last_code,
        attempts=retry.max_retries,
    )


# --- TCP probes -----------------------------------------------------------
def tcp_port_check(host: str, port: int, timeout: float = 5.0) -> bool:
    """Check whether a TCP port is accepting connections.

    Args:
        host: Hostname or IP address.
        port: Port number.
        timeout: Connection timeout in seconds.

    Returns:
        *True* if the connection succeeded.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def wait_for_tcp_port(
    host: str,
    port: int,
    retry: RetryConfig = TCP_RETRY,
    label: str = "",
) -> bool:
    """Wait for a TCP port to become available.

    Args:
        host: Hostname or IP address.
        port: Port number.
        retry: Retry configuration.
        label: Human-readable label for log messages (e.g. "HTTP port
            8080").

    Returns:
        *True* if the port became available.

    Raises:
        HealthCheckError: If the port did not become available within
            the allowed retries.
    """
    display = label or f"{host}:{port}"
    logger.info("Waiting for TCP port: %s", display)

    for attempt in range(1, retry.max_retries + 1):
        if tcp_port_check(host, port):
            logger.info("  TCP port %s is listening ✅", display)
            return True

        if attempt < retry.max_retries:
            time.sleep(retry.interval)
            log_retry_progress(attempt, retry.max_retries, f"waiting for {display}")

    raise HealthCheckError(
        f"TCP port {display} not listening after {retry.max_retries} retries",
        url=display,
        attempts=retry.max_retries,
    )


# --- SSH probe ------------------------------------------------------------
def verify_ssh_service(host: str, port: int = 29418, timeout: int = 10) -> str:
    """Verify that the Gerrit SSH service responds with a host key.

    Uses ``ssh-keyscan`` to contact the SSH service.

    Args:
        host: Hostname or IP address.
        port: SSH port (default 29418).
        timeout: Keyscan timeout in seconds.

    Returns:
        The raw keyscan output (host key lines), or ``""`` if the
        service did not respond.
    """
    try:
        result = subprocess.run(
            ["ssh-keyscan", "-p", str(port), "-T", str(timeout), host],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        output = result.stdout.strip()
        log_ssh_host_key(output, host, port)
        return output
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("ssh-keyscan failed for %s:%d: %s", host, port, exc)
        return ""


# --- Per-instance flows ---------------------------------------------------
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
    wait_for_tcp_port(container_ip, 8080, retry=TCP_RETRY, label="HTTP port 8080")

    # Step 2: SSH port
    logger.info("")
    logger.info("Step 2: Checking SSH port (29418)…")
    wait_for_tcp_port(container_ip, 29418, retry=TCP_SSH_RETRY, label="SSH port 29418")

    # Step 3: SSH service verification
    logger.info("")
    logger.info("Step 3: Verifying Gerrit SSH service…")
    verify_ssh_service(container_ip, port=29418)

    logger.info("")
    logger.info("Replica mode health checks passed ✅")
    return True


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

    health_url = endpoint_url(
        container_ip, effective_api_path, "/config/server/version"
    )

    logger.info("Health check URL: %s", health_url)
    http_health_check(health_url, retry=HTTP_RETRY)

    # Plugin checks
    if not skip_plugin_install:
        logger.info("")
        logger.info("Verifying pull-replication plugin…")
        verify_plugin_loaded(
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


@dataclass
class HealthCheckResult:
    """Result of a health check for a single instance."""

    slug: str
    success: bool = False
    error: str = ""
    is_replica: bool = False


def check_instance(
    docker: DockerManager,
    slug: str,
    instance: dict[str, Any],
    *,
    skip_plugin_install: bool = False,
    use_api_path: bool = False,
) -> HealthCheckResult:
    """Run all health checks for a single Gerrit instance.

    Args:
        docker: Docker CLI wrapper.
        slug: Instance slug.
        instance: Instance metadata dict (from ``instances.json``).
        skip_plugin_install: If *True*, skip plugin verification.
        use_api_path: If *True*, use the ``api_path`` from instance
            metadata.

    Returns:
        The result of the health check.
    """
    result = HealthCheckResult(slug=slug)

    cid = instance.get("cid", "")
    container_ip = instance.get("ip", "")
    effective_api_path = resolve_api_path(instance, use_api_path=use_api_path)

    log_instance_banner(slug, instance, use_api_path=use_api_path)

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
        log_failure_diagnostics(docker, cid, slug)

    return result


# --- Multi-instance orchestrator ------------------------------------------
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

    Args:
        docker: Docker CLI wrapper.
        instance_store: Loaded instance metadata.
        skip_plugin_install: Skip plugin verification if *True*.
        use_api_path: Use API path from instance metadata if *True*.

    Returns:
        Results for each instance, in slug order.

    Raises:
        HealthCheckError: If any instance failed its health check.
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
    log_run_verdict(failed=bool(failed))

    if failed:
        write_summary(failure_summary_markdown())
    else:
        write_status_summary(
            HEALTH_SUMMARY_TITLE,
            HEALTH_SUMMARY_OK_BODY,
            emoji=HEALTH_SUMMARY_OK_EMOJI,
        )

    log_container_status(docker)

    if failed:
        slugs = ", ".join(r.slug for r in failed)
        raise HealthCheckError(
            f"Health checks failed for: {slugs}",
            attempts=len(results),
        )

    return results
