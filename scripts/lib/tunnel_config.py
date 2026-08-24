# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Environment parsing and validation for the tunnel verifier.

Split out of ``verify-tunnel.py``: turns the ``BORE_HOST`` /
``HTTP_PORT`` / ``API_PATH`` family of environment variables into a
single :class:`TunnelSettings` record, and logs the banner that states
what is about to be verified.

Both entry points here take the caller's logger so records keep the
entry point's name, exactly as when the code lived there.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class TunnelSettings:
    """Resolved inputs for one tunnel verification run."""

    bore_host: str
    http_port: str
    port_num: int
    api_path: str
    max_attempts: int
    retry_delay: int

    @property
    def url(self) -> str:
        """Gerrit version endpoint as reached through the tunnel."""
        return (
            f"http://{self.bore_host}:{self.port_num}"
            f"{self.api_path}/config/server/version"
        )


def load_settings(logger: logging.Logger) -> TunnelSettings | None:
    """Read the tunnel settings from the environment.

    Parameters
    ----------
    logger:
        Logger of the calling entry point, used to report the missing
        or malformed variables.

    Returns
    -------
    TunnelSettings | None
        The resolved settings, or ``None`` when a required variable is
        absent or unusable — the caller then exits with status 1.

    Raises
    ------
    ValueError
        If ``MAX_ATTEMPTS`` or ``RETRY_DELAY`` is not an integer.
    """
    bore_host = os.environ.get("BORE_HOST", "").strip()
    http_port = os.environ.get("HTTP_PORT", "").strip()
    api_path = os.environ.get("API_PATH", "").strip()
    use_api_path = os.environ.get("USE_API_PATH", "false").strip().lower() == "true"
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "5"))
    retry_delay = int(os.environ.get("RETRY_DELAY", "3"))

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

    effective_api_path = ""
    if use_api_path and api_path:
        effective_api_path = api_path if api_path.startswith("/") else f"/{api_path}"

    return TunnelSettings(
        bore_host=bore_host,
        http_port=http_port,
        port_num=port_num,
        api_path=effective_api_path,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )


def log_settings(logger: logging.Logger, settings: TunnelSettings) -> None:
    """Log the banner describing what is about to be verified."""
    logger.info("Verifying tunnel connectivity…")
    logger.info("")
    logger.info("  Tunnel host:  %s", settings.bore_host)
    logger.info("  HTTP port:    %s", settings.http_port)
    logger.info("  API path:     %s", settings.api_path or "(none)")
    logger.info("  Target URL:   %s", settings.url)
    logger.info("  Max attempts: %d", settings.max_attempts)
    logger.info("  Retry delay:  %ds", settings.retry_delay)
    logger.info("")
