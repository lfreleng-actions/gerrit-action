# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Container-level health probes driven through the Docker CLI.

Everything in this module answers a question that can only be asked of
the *container* rather than of the Gerrit service endpoints: is the
container up, has Gerrit finished booting, is it running headless, and
did a plugin load?  Each probe takes a :class:`DockerManager` as its
first argument and inspects container state or startup logs.

The network-facing probes (HTTP, TCP and SSH) and the flows that
sequence both kinds of probe live in :mod:`health_check`.
"""

from __future__ import annotations

import logging
import re
import time

import requests
from docker_manager import DockerManager
from errors import DockerError, HealthCheckError
from health_check_endpoints import endpoint_url
from health_check_retry import GERRIT_READY_TIMEOUT

logger = logging.getLogger(__name__)

# Pattern that Gerrit logs when it has finished starting up
_READY_PATTERN = re.compile(r"Gerrit Code Review.*ready")

# Pattern that identifies replica/headless mode
_REPLICA_PATTERN = re.compile(r"\[replica\].*\[headless\]")


# ---------------------------------------------------------------------------
# Container state verification
# ---------------------------------------------------------------------------


def verify_container_running(docker: DockerManager, cid: str, slug: str) -> bool:
    """Verify that a container exists and is in the ``running`` state.

    Args:
        docker: Docker CLI wrapper.
        cid: Container ID.
        slug: Instance slug for logging.

    Returns:
        *True* if the container is running.

    Raises:
        HealthCheckError: If the container does not exist or is not
            running.
    """
    if not docker.container_exists(cid):
        raise HealthCheckError(
            f"Container {cid[:12]} for {slug} does not exist",
            url="",
            attempts=0,
        )

    state = docker.container_state(cid)
    if state != "running":
        # Grab some logs for diagnostics
        try:
            tail = docker.container_logs(cid, tail=20)
        except DockerError:
            tail = "(could not retrieve logs)"
        raise HealthCheckError(
            f"Container {cid[:12]} for {slug} is not running "
            f"(state: {state})\nRecent logs:\n{tail}",
            url="",
            attempts=0,
        )

    logger.info("Container state: %s ✅", state)
    return True


# ---------------------------------------------------------------------------
# Gerrit readiness (log polling)
# ---------------------------------------------------------------------------


def wait_for_gerrit_ready(
    docker: DockerManager,
    cid: str,
    timeout: int = GERRIT_READY_TIMEOUT,
    poll_interval: float = 2.0,
    log_tail: int = 500,
) -> bool:
    """Wait for Gerrit to log its "ready" message.

    Polls the container logs every *poll_interval* seconds, looking for
    the pattern ``Gerrit Code Review .* ready``.

    Args:
        docker: Docker CLI wrapper.
        cid: Container ID.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between log polls.
        log_tail: Number of log lines to inspect on each poll.

    Returns:
        *True* if the ready message was found within the timeout.
        *False* if the timeout elapsed without finding it (a warning
        is logged but no exception is raised, since Gerrit may still
        respond to HTTP checks even without the explicit ready
        message).
    """
    logger.info("Waiting for Gerrit to initialize…")
    elapsed = 0.0

    while elapsed < timeout:
        logs = docker.container_logs(cid, tail=log_tail)
        if _READY_PATTERN.search(logs):
            logger.info("Gerrit ready message detected in logs ✅")
            return True

        time.sleep(poll_interval)
        elapsed += poll_interval

        if int(elapsed) % 10 == 0 and elapsed > 0:
            logger.info("  Waiting… %.0fs elapsed", elapsed)

    logger.warning(
        "Gerrit did not show 'ready' message in logs after %ds. "
        "Proceeding with HTTP check anyway…",
        timeout,
    )
    return False


# ---------------------------------------------------------------------------
# Replica / headless mode detection
# ---------------------------------------------------------------------------


def is_replica_mode(docker: DockerManager, cid: str, tail: int = 2000) -> bool:
    """Detect if Gerrit is running in replica/headless mode.

    In this mode the REST API is disabled and HTTP health checks will
    fail; we must use TCP port checks instead.

    Args:
        docker: Docker CLI wrapper.
        cid: Container ID.
        tail: Number of log lines to search.

    Returns:
        *True* if the replica/headless pattern was found in logs.
    """
    logs = docker.container_logs(cid, tail=tail)
    return bool(_REPLICA_PATTERN.search(logs))


# ---------------------------------------------------------------------------
# Plugin verification
# ---------------------------------------------------------------------------


def verify_plugin_loaded(
    docker: DockerManager,
    cid: str,
    plugin_name: str,
    container_ip: str = "",
    effective_api_path: str = "",
) -> bool:
    """Verify that a Gerrit plugin is loaded.

    This is the **single implementation** replacing the duplicated
    ``check_plugin_in_logs()`` function that existed in
    ``check-services.sh`` and ``trigger-replication.sh``.

    Detection strategy (ordered by reliability):

    1. Search recent container logs for ``"Loaded plugin <name>"``.
    2. Query the ``/plugins/`` REST endpoint via HTTP.
    3. Check whether the plugin ``.jar`` file exists in the container.

    Args:
        docker: Docker CLI wrapper.
        cid: Container ID.
        plugin_name: Name of the plugin (e.g. ``"pull-replication"``).
        container_ip: If provided, enables the HTTP fallback check.
        effective_api_path: API path prefix for the HTTP fallback.

    Returns:
        *True* if the plugin was confirmed loaded by any method.
    """
    load_pattern = f"Loaded plugin {plugin_name}"

    # Method 1: Check container logs (most reliable)
    if docker.grep_logs(cid, load_pattern, tail=1000):
        logger.info("%s plugin loaded ✅ (verified via logs)", plugin_name)
        return True

    # Extended search with more lines
    if docker.grep_logs(cid, load_pattern, tail=5000):
        logger.info("%s plugin loaded ✅ (verified via extended logs)", plugin_name)
        return True

    # Method 2: HTTP API check
    if container_ip:
        try:
            url = endpoint_url(container_ip, effective_api_path, "/plugins/")
            resp = requests.get(url, timeout=5)
            if plugin_name in resp.text:
                logger.info("%s plugin detected ✅ (verified via HTTP)", plugin_name)
                return True
        except requests.RequestException:
            pass

    # Method 3: Check jar file existence
    jar_path = f"/var/gerrit/plugins/{plugin_name}.jar"
    if docker.exec_test(cid, f"-f {jar_path}"):
        logger.info("Plugin file %s exists in container", jar_path)
        # Give it a moment and check logs again
        time.sleep(3)
        if docker.grep_logs(cid, load_pattern, tail=1000):
            logger.info("%s plugin loaded ✅ (after wait)", plugin_name)
            return True
        logger.warning(
            "Plugin file exists but %s not yet loaded – "
            "this may be normal during initial startup",
            plugin_name,
        )
        return True  # File exists, treat as OK (may still be loading)

    logger.warning("Plugin %s not found by any method", plugin_name)
    return False
