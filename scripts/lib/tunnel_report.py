# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Target resolution and operator-facing reporting for tunnel checks.

This module owns everything *around* the probe loop in
``scripts/verify-tunnel.py``: turning the environment into a resolved
:class:`TunnelTarget`, rendering per-attempt failure detail, and
emitting the success / failure narrative (logs, GitHub Actions
annotation, and ``GITHUB_STEP_SUMMARY`` entries).

The probing itself, the DNS/TCP diagnostics and the retry loop stay in
the entry-point script so the symbols the test-suite patches
(``probe_url``, ``diagnose_host``, ``requests``, ``socket``) remain
module attributes of ``verify_tunnel``.  ``report_tunnel_failure``
therefore takes the diagnostic probe as a callable rather than
importing one: this both preserves that patch surface *and* keeps the
DNS/TCP round-trips lazily sequenced after the failure header is
logged, so an operator sees the summary immediately instead of waiting
on connect timeouts.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from tunnel_probe import TunnelCheckResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TunnelTarget:
    """A fully-resolved tunnel endpoint plus its retry policy.

    Attributes
    ----------
    host:
        Tunnel hostname from ``BORE_HOST``.
    port:
        Tunnel HTTP port from ``HTTP_PORT``, validated as an integer.
    api_path:
        The *effective* API path — empty unless ``USE_API_PATH`` is
        enabled, and always normalised to a leading slash.
    url:
        The Gerrit version endpoint the probe loop hits.
    max_attempts:
        Number of probe attempts from ``MAX_ATTEMPTS``.
    retry_delay:
        Seconds slept between attempts, from ``RETRY_DELAY``.
    """

    host: str
    port: int
    api_path: str
    url: str
    max_attempts: int
    retry_delay: int


def resolve_tunnel_target() -> TunnelTarget | None:
    """Build a :class:`TunnelTarget` from the environment.

    Returns
    -------
    TunnelTarget | None
        ``None`` when a required variable is missing or malformed; the
        reason has already been reported to the log and, for missing
        variables, as a GitHub Actions error annotation.
    """
    bore_host = os.environ.get("BORE_HOST", "").strip()
    http_port = os.environ.get("HTTP_PORT", "").strip()
    api_path = os.environ.get("API_PATH", "").strip()
    use_api_path = os.environ.get("USE_API_PATH", "false").strip().lower() == "true"
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "5"))
    retry_delay = int(os.environ.get("RETRY_DELAY", "3"))

    # --- Validate inputs ---
    if not bore_host:
        logger.error("BORE_HOST is not set — cannot verify tunnel")
        print("::error::BORE_HOST environment variable is required", file=sys.stderr)
        return None

    if not http_port:
        logger.error("HTTP_PORT is not set — cannot verify tunnel")
        print("::error::HTTP_PORT environment variable is required", file=sys.stderr)
        return None

    try:
        port_num = int(http_port)
    except ValueError:
        logger.error("HTTP_PORT is not a valid integer: %r", http_port)
        return None

    # --- Build URL ---
    effective_api_path = ""
    if use_api_path and api_path:
        effective_api_path = api_path if api_path.startswith("/") else f"/{api_path}"

    url = f"http://{bore_host}:{port_num}{effective_api_path}/config/server/version"

    return TunnelTarget(
        host=bore_host,
        port=port_num,
        api_path=effective_api_path,
        url=url,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )


def log_tunnel_target(target: TunnelTarget) -> None:
    """Log the resolved target before the probe loop starts."""
    logger.info("Verifying tunnel connectivity…")
    logger.info("")
    logger.info("  Tunnel host:  %s", target.host)
    logger.info("  HTTP port:    %s", target.port)
    logger.info("  API path:     %s", target.api_path or "(none)")
    logger.info("  Target URL:   %s", target.url)
    logger.info("  Max attempts: %d", target.max_attempts)
    logger.info("  Retry delay:  %ds", target.retry_delay)
    logger.info("")


def format_attempt_detail(result: TunnelCheckResult) -> str:
    """Render one failed attempt as a single diagnostic string."""
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


def report_tunnel_success(
    target: TunnelTarget,
    result: TunnelCheckResult,
    attempt: int,
) -> None:
    """Log a successful probe and record it in the step summary."""
    # Parse the Gerrit version from the response body.
    # Gerrit wraps JSON responses in )]}' prefix.
    version = result.body
    for prefix in (")]}'", ")]}'\n", '"'):
        version = version.lstrip(prefix)
    version = version.strip().strip('"')

    logger.info("")
    logger.info("  Tunnel verified ✅ (Gerrit %s)", version)
    logger.info("  Response time: %.0fms", result.elapsed_ms)
    logger.info("")

    # Write success to step summary if available
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a") as fh:
                fh.write(
                    f"**Tunnel Verification** ✅\n\n"
                    f"- URL: `{target.url}`\n"
                    f"- Gerrit version: `{version}`\n"
                    f"- Response time: {result.elapsed_ms:.0f}ms\n"
                    f"- Attempt: {attempt}/{target.max_attempts}\n\n"
                )
        except OSError as exc:
            logger.debug("Could not write step summary: %s", exc)


def report_tunnel_failure(
    target: TunnelTarget,
    last_result: TunnelCheckResult | None,
    attempt_history: list[str],
    diagnose: Callable[[str, int], list[str]],
) -> None:
    """Report exhausted retries with full diagnostics.

    Parameters
    ----------
    target:
        The endpoint that could not be reached.
    last_result:
        The final probe outcome, or ``None`` if none was recorded.
    attempt_history:
        One rendered line per failed attempt, in attempt order.
    diagnose:
        Callable returning DNS/TCP diagnostic lines for a host/port.
        Invoked at the point its output is logged so the failure
        header is not delayed by connect timeouts.
    """
    _log_failure_header(target, last_result)

    # Network diagnostics on failure
    logger.error("")
    logger.error("  Network diagnostics:")
    for line in diagnose(target.host, target.port):
        logger.error("    %s", line)

    _log_possible_causes(target, last_result)

    logger.error("")

    # Attempt history
    logger.error("  Attempt history:")
    for entry in attempt_history:
        logger.error("    %s", entry)
    logger.error("")

    _print_failure_annotation(target, last_result)
    _write_failure_summary(target, last_result)


def _log_failure_header(
    target: TunnelTarget, last_result: TunnelCheckResult | None
) -> None:
    """Log the headline failure summary and the last error seen."""
    logger.error("")
    logger.error(
        "  ❌ Tunnel verification failed after %d attempts", target.max_attempts
    )
    logger.error("")
    logger.error("  URL: %s", target.url)
    if last_result:
        logger.error("  Last error type: %s", last_result.error_type)
        if last_result.status_code is not None:
            logger.error("  Last HTTP status: %d", last_result.status_code)
        if last_result.error:
            logger.error("  Last error: %s", last_result.error[:300])


def _log_possible_causes(
    target: TunnelTarget, last_result: TunnelCheckResult | None
) -> None:
    """Log the common failure explanations for the observed error type."""
    # Common failure explanations
    logger.error("")
    logger.error("  Possible causes:")
    if last_result and last_result.error_type == "connection_refused":
        logger.error("    - Bore tunnel process may have exited or not started")
        logger.error("    - The assigned port may have been reclaimed by bore.pub")
        logger.error("    - Gerrit container may not be listening on the expected port")
    elif last_result and last_result.error_type == "dns_failure":
        logger.error("    - DNS resolution failed for %s", target.host)
        logger.error("    - Check network connectivity and DNS configuration")
    elif last_result and last_result.error_type == "timeout":
        logger.error("    - Connection timed out — tunnel may be overloaded or down")
        logger.error("    - Gerrit may still be starting up")
    elif last_result and last_result.error_type == "http_error":
        logger.error("    - Gerrit is reachable but returned an error")
        logger.error(
            "    - Check API path configuration (current: %s)",
            target.api_path or "(none)",
        )
        if last_result.status_code == 404:
            logger.error("    - HTTP 404 suggests the API path is incorrect")
        elif last_result.status_code == 401:
            logger.error("    - HTTP 401 suggests authentication is required")
    else:
        logger.error("    - Unexpected error — check logs above for details")


def _print_failure_annotation(
    target: TunnelTarget, last_result: TunnelCheckResult | None
) -> None:
    """Emit the GitHub Actions error annotation for the failure."""
    # GitHub Actions error annotation
    error_msg = f"Tunnel verification failed after {target.max_attempts} attempts"
    if last_result:
        error_msg += f" (last: {last_result.error_type}"
        if last_result.status_code is not None:
            error_msg += f", HTTP {last_result.status_code}"
        error_msg += ")"
    print(f"::error::{error_msg}", file=sys.stderr)


def _write_failure_summary(
    target: TunnelTarget, last_result: TunnelCheckResult | None
) -> None:
    """Append the failure block to ``GITHUB_STEP_SUMMARY`` if set."""
    # Write failure to step summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            lines = [
                "**Tunnel Verification** ❌\n",
                "",
                f"Failed to verify tunnel connectivity after "
                f"{target.max_attempts} attempts.\n",
                "",
                f"- URL: `{target.url}`",
                f"- Last error: `{last_result.error_type if last_result else 'unknown'}`",
            ]
            if last_result and last_result.status_code is not None:
                lines.append(f"- HTTP status: `{last_result.status_code}`")
            lines.append("")
            with open(summary_file, "a") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as exc:
            logger.debug("Could not write step summary: %s", exc)
