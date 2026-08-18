# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Writing an instance's ``gerrit.config``.

:func:`configure_gerrit` drives ``git config -f etc/gerrit.config`` to
turn a freshly-initialised site into the throw-away review server this
action provisions: development-mode auth, the OOTB first-time redirect,
the advertised URLs resolved by :mod:`startup_endpoints`, and the
pull-replication plugin switch.

Auth is deliberately ``DEVELOPMENT_BECOME_ANY_ACCOUNT``, so the module
also owns the exposure decision that goes with it — remote plugin admin
is disabled whenever the instance is reachable through a tunnel, and the
warning is escalated when that tunnel is on a public network.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from startup_endpoints import InstanceEndpoints, is_private_tunnel

logger = logging.getLogger(__name__)


def configure_gerrit(
    instance_dir: Path,
    slug: str,
    endpoints: InstanceEndpoints,
    api_path: str,
    tunnel_host: str = "",
) -> None:
    """Write ``gerrit.config`` settings via ``git config``.

    This mirrors the ``configure_gerrit()`` function from the shell
    script, setting auth to DEVELOPMENT_BECOME_ANY_ACCOUNT mode and
    configuring pull-replication.

    Parameters
    ----------
    instance_dir:
        Host-side site directory holding ``etc/gerrit.config``.
    slug:
        Instance slug, written as ``gerrit.instanceId``.
    endpoints:
        Resolved addressing for the instance — supplies the canonical
        web URL, the container-side listen URL, the advertised SSH
        address and whether a tunnel is in play.
    api_path:
        URL prefix mirrored from the production server, used for the
        OOTB first-time redirect.
    tunnel_host:
        Tunnel address, used only to decide how loudly to warn about
        development-mode auth being reachable through it.
    """
    logger.info("Configuring Gerrit for %s…", slug)

    config_file = str(instance_dir / "etc" / "gerrit.config")

    def _gc(*args: str) -> None:
        """Run ``git config -f <config_file> <args…>``."""
        subprocess.run(
            ["git", "config", "-f", config_file, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    if api_path:
        logger.info("  URL prefix: %s (mirroring production server)", api_path)
    else:
        logger.info("  URL prefix: (none)")

    # Core settings
    _gc("gerrit.instanceId", slug)
    _gc("gerrit.canonicalWebUrl", endpoints.canonical_url)
    _gc("httpd.listenUrl", endpoints.listen_url)
    _gc("sshd.listenAddress", "*:29418")
    _gc("sshd.advertisedAddress", endpoints.advertised_ssh_addr)

    # Download schemes
    _gc("download.scheme", "ssh")
    _gc("--add", "download.scheme", "http")
    _gc("download.command", "checkout")
    _gc("--add", "download.command", "cherry_pick")
    _gc("--add", "download.command", "pull")

    # Auth — development mode for testing
    _gc("auth.type", "DEVELOPMENT_BECOME_ANY_ACCOUNT")

    # OOTB filter for automatic account creation
    _gc(
        "httpd.filterClass",
        "com.googlesource.gerrit.plugins.ootb.FirstTimeRedirect",
    )
    ootb_redirect_url = f"{api_path}/login/%23%2F?account_id=1000000"
    _gc("httpd.firstTimeRedirectUrl", ootb_redirect_url)

    # Remote plugin admin
    if endpoints.use_tunnel:
        _gc("plugins.allowRemoteAdmin", "false")
        _log_tunnel_exposure(tunnel_host)
    else:
        _gc("plugins.allowRemoteAdmin", "true")

    _gc("container.user", "gerrit")
    _gc("plugin.pull-replication.enabled", "true")

    logger.info("Gerrit configured ✅")
    logger.info("  Mode: non-replica (web UI enabled)")
    logger.info("  Replication: fetchEvery polling")


def _log_tunnel_exposure(tunnel_host: str) -> None:
    """Report how exposed development-mode auth is on this tunnel."""
    if is_private_tunnel(tunnel_host):
        logger.info("Tunnel mode active (private network — remote admin disabled).")
        return

    logger.warning("⚠️  Tunnel mode active with DEVELOPMENT_BECOME_ANY_ACCOUNT auth.")
    logger.warning("   Anyone with network access can authenticate as any user.")
    logger.warning("   Remote plugin admin has been disabled to limit exposure.")
