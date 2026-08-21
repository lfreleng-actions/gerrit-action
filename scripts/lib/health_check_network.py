# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Network-level Gerrit health probes.

The checks in this module talk to a Gerrit instance over the network
rather than through Docker: an HTTP probe of the REST API, a plain TCP
connect, and an ``ssh-keyscan`` of the SSH service.  They are
re-exported from :mod:`health_check`.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import time

import requests
from errors import HealthCheckError
from health_check_model import (
    _HEALTHY_HTTP_CODES,
    HTTP_RETRY,
    RetryConfig,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP health check
# ---------------------------------------------------------------------------


def http_health_check(
    url: str,
    retry: RetryConfig = HTTP_RETRY,
) -> int:
    """Perform an HTTP health check with retries.

    The check succeeds when the endpoint responds with one of the
    "healthy" status codes (200, 401, 403).

    Parameters
    ----------
    url:
        Full URL to check (e.g.
        ``http://10.0.0.2:8080/config/server/version``).
    retry:
        Retry configuration.

    Returns
    -------
    int
        The HTTP status code that passed the check.

    Raises
    ------
    HealthCheckError
        If the endpoint did not return a healthy status code within
        the allowed retries.
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
            if attempt % 5 == 0:
                logger.info(
                    "  Retry %d/%d (HTTP code: %s)",
                    attempt,
                    retry.max_retries,
                    last_code or "N/A",
                )

    raise HealthCheckError(
        f"HTTP health check failed for {url} after {retry.max_retries} retries "
        f"(last HTTP code: {last_code})",
        url=url,
        last_status_code=last_code,
        attempts=retry.max_retries,
    )


# ---------------------------------------------------------------------------
# TCP port check
# ---------------------------------------------------------------------------


def tcp_port_check(
    host: str,
    port: int,
    timeout: float = 5.0,
) -> bool:
    """Check whether a TCP port is accepting connections.

    Parameters
    ----------
    host:
        Hostname or IP address.
    port:
        Port number.
    timeout:
        Connection timeout in seconds.

    Returns
    -------
    bool
        *True* if the connection succeeded.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# SSH keyscan verification
# ---------------------------------------------------------------------------


def verify_ssh_service(
    host: str,
    port: int = 29418,
    timeout: int = 10,
) -> str:
    """Verify that the Gerrit SSH service responds with a host key.

    Uses ``ssh-keyscan`` to contact the SSH service.

    Parameters
    ----------
    host:
        Hostname or IP address.
    port:
        SSH port (default 29418).
    timeout:
        Keyscan timeout in seconds.

    Returns
    -------
    str
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
        if output:
            # Show a truncated version of the first key for logging
            first_line = output.splitlines()[0]
            parts = first_line.split()
            key_preview = (
                f"{parts[1]} {parts[2][:40]}…" if len(parts) >= 3 else first_line[:60]
            )
            logger.info("  Gerrit SSH service is responding ✅")
            logger.info("  Host key received: %s", key_preview)
        else:
            logger.warning(
                "Could not retrieve SSH host key from %s:%d. "
                "SSH port is open but service may not be fully ready.",
                host,
                port,
            )
        return output
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("ssh-keyscan failed for %s:%d: %s", host, port, exc)
        return ""
