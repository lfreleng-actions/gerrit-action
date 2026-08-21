# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verification reporting for the tunnel verifier.

Split out of ``verify-tunnel.py``: renders the outcome of the retry
loop — the verified-tunnel banner, the per-attempt failure lines, and
the diagnostic dump that follows an exhausted retry loop — along with
the matching GitHub Actions annotations and step-summary entries.

Every entry point takes the caller's logger so records keep the entry
point's name, exactly as when the code lived there.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

from tunnel_config import TunnelSettings
from tunnel_model import TunnelCheckResult


def parse_gerrit_version(body: str) -> str:
    """Extract the Gerrit version from a version endpoint response body.

    Gerrit wraps JSON responses in a ``)]}'`` prefix.
    """
    version = body
    for prefix in (")]}'", ")]}'\n", '"'):
        version = version.lstrip(prefix)
    return version.strip().strip('"')


def format_attempt_detail(result: TunnelCheckResult) -> str:
    """Render one failed probe as a single diagnostic line."""
    detail = f"[{result.error_type}]"
    if result.status_code is not None:
        detail += f" HTTP {result.status_code}"
    if result.error:
        # Truncate for readability but keep enough for debugging
        error_msg = result.error[:200]
        detail += f" — {error_msg}"
    if result.body:
        detail += f"\n    Response body: {result.body[:200]}"
    return detail


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def report_success(
    logger: logging.Logger,
    settings: TunnelSettings,
    result: TunnelCheckResult,
    attempt: int,
) -> None:
    """Report a verified tunnel and record it in the step summary."""
    version = parse_gerrit_version(result.body)

    logger.info("")
    logger.info("  Tunnel verified ✅ (Gerrit %s)", version)
    logger.info("  Response time: %.0fms", result.elapsed_ms)
    logger.info("")

    _write_success_summary(logger, settings, result, version, attempt)


def _write_success_summary(
    logger: logging.Logger,
    settings: TunnelSettings,
    result: TunnelCheckResult,
    version: str,
    attempt: int,
) -> None:
    """Write the success entry to the step summary, if one is available."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    try:
        with open(summary_file, "a") as fh:
            fh.write(
                f"**Tunnel Verification** ✅\n\n"
                f"- URL: `{settings.url}`\n"
                f"- Gerrit version: `{version}`\n"
                f"- Response time: {result.elapsed_ms:.0f}ms\n"
                f"- Attempt: {attempt}/{settings.max_attempts}\n\n"
            )
    except OSError as exc:
        logger.debug("Could not write step summary: %s", exc)


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


def report_failure(
    logger: logging.Logger,
    settings: TunnelSettings,
    last_result: TunnelCheckResult | None,
    diagnose: Callable[[], list[str]],
    error_summary: list[str],
) -> None:
    """Report an exhausted retry loop with full diagnostics.

    *diagnose* is called at the point its output is logged, so the
    network probing still happens after the failure banner rather than
    before it.
    """
    logger.error("")
    logger.error(
        "  ❌ Tunnel verification failed after %d attempts", settings.max_attempts
    )
    logger.error("")
    logger.error("  URL: %s", settings.url)
    if last_result:
        logger.error("  Last error type: %s", last_result.error_type)
        if last_result.status_code is not None:
            logger.error("  Last HTTP status: %d", last_result.status_code)
        if last_result.error:
            logger.error("  Last error: %s", last_result.error[:300])

    # Network diagnostics on failure
    logger.error("")
    logger.error("  Network diagnostics:")
    for line in diagnose():
        logger.error("    %s", line)

    _log_failure_causes(logger, last_result, settings)

    # Attempt history
    logger.error("  Attempt history:")
    for entry in error_summary:
        logger.error("    %s", entry)
    logger.error("")

    # GitHub Actions error annotation
    print(f"::error::{_failure_annotation(settings, last_result)}", file=sys.stderr)

    _write_failure_summary(logger, settings, last_result)


def _log_failure_causes(
    logger: logging.Logger,
    last_result: TunnelCheckResult | None,
    settings: TunnelSettings,
) -> None:
    """Log the common explanations for the observed error type."""
    logger.error("")
    logger.error("  Possible causes:")
    if last_result and last_result.error_type == "connection_refused":
        logger.error("    - Bore tunnel process may have exited or not started")
        logger.error("    - The assigned port may have been reclaimed by bore.pub")
        logger.error("    - Gerrit container may not be listening on the expected port")
    elif last_result and last_result.error_type == "dns_failure":
        logger.error("    - DNS resolution failed for %s", settings.bore_host)
        logger.error("    - Check network connectivity and DNS configuration")
    elif last_result and last_result.error_type == "timeout":
        logger.error("    - Connection timed out — tunnel may be overloaded or down")
        logger.error("    - Gerrit may still be starting up")
    elif last_result and last_result.error_type == "http_error":
        logger.error("    - Gerrit is reachable but returned an error")
        logger.error(
            "    - Check API path configuration (current: %s)",
            settings.api_path or "(none)",
        )
        if last_result.status_code == 404:
            logger.error("    - HTTP 404 suggests the API path is incorrect")
        elif last_result.status_code == 401:
            logger.error("    - HTTP 401 suggests authentication is required")
    else:
        logger.error("    - Unexpected error — check logs above for details")

    logger.error("")


def _failure_annotation(
    settings: TunnelSettings,
    last_result: TunnelCheckResult | None,
) -> str:
    """Build the ``::error::`` annotation text for a failed run."""
    error_msg = f"Tunnel verification failed after {settings.max_attempts} attempts"
    if last_result:
        error_msg += f" (last: {last_result.error_type}"
        if last_result.status_code is not None:
            error_msg += f", HTTP {last_result.status_code}"
        error_msg += ")"
    return error_msg


def _write_failure_summary(
    logger: logging.Logger,
    settings: TunnelSettings,
    last_result: TunnelCheckResult | None,
) -> None:
    """Write the failure entry to the step summary, if one is available."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    try:
        lines = [
            "**Tunnel Verification** ❌\n",
            "",
            f"Failed to verify tunnel connectivity after {settings.max_attempts} attempts.\n",
            "",
            f"- URL: `{settings.url}`",
            f"- Last error: `{last_result.error_type if last_result else 'unknown'}`",
        ]
        if last_result and last_result.status_code is not None:
            lines.append(f"- HTTP status: `{last_result.status_code}`")
        lines.append("")
        with open(summary_file, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        logger.debug("Could not write step summary: %s", exc)
