#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Verify tunnel connectivity to a Gerrit instance.

Replaces the inline shell ``curl`` loop in the workflow with a Python
script that provides **comprehensive diagnostic output** on failure —
HTTP status codes, connection error details, DNS resolution info, and
retry progress — instead of a bare ``exit code 7``.

Usage::

    # From a GitHub Actions workflow step
    python scripts/verify-tunnel.py

    # Locally with environment variables
    BORE_HOST=bore.pub HTTP_PORT=60479 \\
        python scripts/verify-tunnel.py

    # With API path
    BORE_HOST=bore.pub HTTP_PORT=60479 \\
    API_PATH=/infra USE_API_PATH=true \\
        python scripts/verify-tunnel.py

Environment Variables
---------------------
BORE_HOST
    Tunnel hostname (e.g. ``bore.pub``).
HTTP_PORT
    Tunnel HTTP port number.
API_PATH
    Optional API path prefix (e.g. ``/infra``, ``/r``).
USE_API_PATH
    If ``"true"`` and ``API_PATH`` is set, include the API path in
    the URL.
MAX_ATTEMPTS
    Number of retry attempts (default: ``5``).
RETRY_DELAY
    Seconds between retries (default: ``3``).
DEBUG
    If ``"true"``, enable verbose diagnostic output.

The supporting pieces live in focused modules under ``scripts/lib`` and
are re-exported here, so this file stays a thin entry point:

* :mod:`tunnel_model` — the per-probe result record
* :mod:`tunnel_probe_urllib` — the standard-library probe fallback
* :mod:`tunnel_config` — environment parsing, validation and banner
* :mod:`tunnel_report` — success and failure reporting

The ``requests``-backed probe, the DNS/TCP diagnostics and the retry
loop stay here: they bind the optional ``requests`` import and the
``socket`` module, and :func:`run` resolves every probe as an attribute
of this module, which is how callers substitute them.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup – ensure ``scripts/lib`` is importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from logging_utils import setup_logging  # noqa: E402
from tunnel_config import load_settings, log_settings  # noqa: E402
from tunnel_model import TunnelCheckResult  # noqa: E402
from tunnel_probe_urllib import _probe_with_urllib  # noqa: E402
from tunnel_report import (  # noqa: E402
    format_attempt_detail,
    report_failure,
    report_success,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TunnelCheckResult",
    # The underscore-prefixed probes are long-standing internals that
    # callers (notably the test suite) reach through this module; they
    # are listed so the re-export stays explicit.
    "_probe_with_requests",
    "_probe_with_urllib",
    "diagnose_host",
    "main",
    "probe_url",
    "run",
]

# We use requests if available (it's a project dependency), but fall
# back to urllib so the script can also run in minimal environments.
try:
    import requests  # noqa: F401

    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _probe_with_requests(url: str, timeout: float = 10.0) -> TunnelCheckResult:
    """Probe *url* using the ``requests`` library."""
    start = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=True)  # pyright: ignore[reportPossiblyUnbound, reportPossiblyUnboundVariable]
        elapsed = (time.monotonic() - start) * 1000

        body = resp.text.strip()
        if resp.status_code == 200:
            return TunnelCheckResult(
                success=True,
                status_code=resp.status_code,
                body=body,
                elapsed_ms=elapsed,
            )

        return TunnelCheckResult(
            success=False,
            status_code=resp.status_code,
            body=body[:500],
            error=f"HTTP {resp.status_code} {resp.reason}",
            error_type="http_error",
            elapsed_ms=elapsed,
        )

    except requests.exceptions.ConnectionError as exc:  # pyright: ignore[reportPossiblyUnbound, reportPossiblyUnboundVariable]
        elapsed = (time.monotonic() - start) * 1000
        inner = str(exc)
        if "Connection refused" in inner or "ConnectTimeoutError" in inner:
            error_type = "connection_refused"
        elif "Name or service not known" in inner or "getaddrinfo" in inner:
            error_type = "dns_failure"
        else:
            error_type = "connection_error"
        return TunnelCheckResult(
            success=False,
            error=inner[:300],
            error_type=error_type,
            elapsed_ms=elapsed,
        )

    except requests.exceptions.Timeout as exc:  # pyright: ignore[reportPossiblyUnbound, reportPossiblyUnboundVariable]
        elapsed = (time.monotonic() - start) * 1000
        return TunnelCheckResult(
            success=False,
            error=str(exc)[:300],
            error_type="timeout",
            elapsed_ms=elapsed,
        )

    except requests.exceptions.RequestException as exc:  # pyright: ignore[reportPossiblyUnbound, reportPossiblyUnboundVariable]
        elapsed = (time.monotonic() - start) * 1000
        return TunnelCheckResult(
            success=False,
            error=str(exc)[:300],
            error_type="request_error",
            elapsed_ms=elapsed,
        )


def probe_url(url: str, timeout: float = 10.0) -> TunnelCheckResult:
    """Probe *url* using the best available HTTP client."""
    if _HAS_REQUESTS:
        return _probe_with_requests(url, timeout=timeout)
    return _probe_with_urllib(url, timeout=timeout)


# ---------------------------------------------------------------------------
# DNS / network diagnostics
# ---------------------------------------------------------------------------


def diagnose_host(host: str, port: int) -> list[str]:
    """Return a list of diagnostic strings about *host*:*port*.

    Performs DNS resolution and a raw TCP connect to gather information
    that helps debug tunnel failures.
    """
    diag: list[str] = []

    # DNS resolution
    try:
        addrs = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        unique_ips = sorted({str(addr[4][0]) for addr in addrs})
        diag.append(f"DNS resolution: {host} -> {', '.join(unique_ips)}")
    except socket.gaierror as exc:
        diag.append(f"DNS resolution FAILED: {exc}")
        return diag  # no point trying TCP if DNS fails

    # Raw TCP connect (use create_connection to handle IPv4/IPv6)
    for ip in unique_ips[:3]:
        try:
            sock = socket.create_connection((ip, port), timeout=5.0)
            sock.close()
            diag.append(f"TCP connect to {ip}:{port}: OK")
        except OSError as exc:
            diag.append(f"TCP connect to {ip}:{port}: FAILED ({exc})")

    return diag


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> int:
    """Run the tunnel verification with retries and diagnostics.

    Returns
    -------
    int
        Exit code: 0 on success, 1 on failure.
    """
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    setup_logging(debug=debug)

    settings = load_settings(logger)
    if settings is None:
        return 1

    log_settings(logger, settings)

    # --- Pre-flight diagnostics ---
    if debug:
        logger.debug("Running pre-flight network diagnostics…")
        for line in diagnose_host(settings.bore_host, settings.port_num):
            logger.debug("  %s", line)
        logger.debug("")

    # --- Retry loop ---
    last_result: TunnelCheckResult | None = None
    error_summary: list[str] = []

    for attempt in range(1, settings.max_attempts + 1):
        logger.info("  Attempt %d/%d: %s", attempt, settings.max_attempts, settings.url)

        result = probe_url(settings.url, timeout=10.0)
        last_result = result

        if result.success:
            report_success(logger, settings, result, attempt)
            return 0

        detail = format_attempt_detail(result)
        logger.warning("    FAILED: %s (%.0fms)", detail, result.elapsed_ms)
        error_summary.append(f"Attempt {attempt}: {detail}")

        if attempt < settings.max_attempts:
            logger.info("    Retrying in %ds…", settings.retry_delay)
            time.sleep(settings.retry_delay)

    # --- All attempts exhausted ---
    report_failure(
        logger,
        settings,
        last_result,
        lambda: diagnose_host(settings.bore_host, settings.port_num),
        error_summary,
    )
    return 1


def main() -> int:
    """Entry point with top-level error handling."""
    try:
        return run()
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130
    except Exception as exc:
        logger.exception("Unexpected error during tunnel verification: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
