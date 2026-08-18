# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Environment-derived configuration for the local replication harness.

Owns the two things the harness reads from its surroundings rather
than from the CLI: the tunable run settings (:class:`HarnessSettings`)
and the Gerrit HTTP Basic credentials.

Bundling the settings into a single frozen value keeps them travelling
together through the scenario runner instead of being threaded as a
long tail of positional arguments, and makes it obvious that nothing
downstream may mutate them mid-run.
"""

from __future__ import annotations

import dataclasses
import logging
import netrc
import os
import sys

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class HarnessSettings:
    """Tunables that apply to every scenario in a harness run.

    Attributes
    ----------
    gerrit_version:
        Gerrit Docker image tag (``GERRIT_VERSION``).
    plugin_version:
        Pull-replication plugin branch (``PLUGIN_VERSION``).
    timeout:
        Per-scenario replication timeout in seconds
        (``REPLICATION_WAIT_TIMEOUT``).
    stability_window:
        Seconds of no observable change before the wait loop declares
        replication quiescent (``STABILITY_WINDOW``).
    fetch_every:
        Pull-replication poll interval, as a Gerrit interval string
        such as ``"15s"`` (``FETCH_EVERY``).
    debug:
        Verbose output, resolved by the caller from ``DEBUG`` before
        logging is configured.
    """

    gerrit_version: str
    plugin_version: str
    timeout: int
    stability_window: int
    fetch_every: str
    debug: bool

    @classmethod
    def from_env(cls, *, debug: bool) -> HarnessSettings:
        """Build settings from the environment.

        Parameters
        ----------
        debug:
            The already-resolved ``DEBUG`` flag, passed in because the
            caller needs it before logging is configured.
        """
        return cls(
            gerrit_version=os.environ.get("GERRIT_VERSION", "3.13.1-ubuntu24"),
            plugin_version=os.environ.get("PLUGIN_VERSION", "stable-3.13"),
            timeout=int(os.environ.get("REPLICATION_WAIT_TIMEOUT", "180")),
            stability_window=int(os.environ.get("STABILITY_WINDOW", "30")),
            fetch_every=os.environ.get("FETCH_EVERY", "15s"),
            debug=debug,
        )


def log_settings(settings: HarnessSettings) -> None:
    """Log the resolved run configuration."""
    logger.info("Test configuration:")
    logger.info("  Gerrit version:     %s", settings.gerrit_version)
    logger.info("  Plugin version:     %s", settings.plugin_version)
    logger.info("  Timeout:            %ds", settings.timeout)
    logger.info("  Stability window:   %ds", settings.stability_window)
    logger.info("  Fetch every:        %s", settings.fetch_every)
    logger.info("  Debug:              %s", settings.debug)
    logger.info("")


def resolve_credentials(host: str) -> tuple[str, str]:
    """Resolve HTTP Basic credentials from env vars or ~/.netrc.

    Returns ``(username, password)`` or raises ``SystemExit``.
    """
    user = os.environ.get("GERRIT_HTTP_USERNAME", "").strip()
    password = os.environ.get("GERRIT_HTTP_PASSWORD", "").strip()
    if user and password:
        return user, password

    # Try ~/.netrc
    try:
        nrc = netrc.netrc()
        auth = nrc.authenticators(host)
        if auth and auth[2] is not None:
            return auth[0], auth[2]
    except (FileNotFoundError, netrc.NetrcParseError):
        pass

    logger.error(
        "No credentials found for %s.  Set GERRIT_HTTP_USERNAME / "
        "GERRIT_HTTP_PASSWORD or add an entry to ~/.netrc.",
        host,
    )
    sys.exit(1)
