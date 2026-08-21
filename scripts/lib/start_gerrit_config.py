# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The ``etc`` files a Gerrit instance is configured through.

Split out of ``start-instances.py``.  Writes ``gerrit.config`` (via
``git config``) and the matching ``secure.config`` credentials, plus the
private-address test that decides how loudly tunnel mode warns.  All
three are re-exported from ``start-instances.py``.
"""

from __future__ import annotations

import ipaddress
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from config import ActionConfig
from start_model import GerritConfigOptions

logger = logging.getLogger(__name__)

# A single ``git config -f <gerrit.config> <args…>`` invocation.
GitConfig = Callable[..., None]


def configure_gerrit(instance_dir: Path, options: GerritConfigOptions) -> None:
    """Write ``gerrit.config`` settings via ``git config``.

    This mirrors the ``configure_gerrit()`` function from the shell
    script, setting auth to DEVELOPMENT_BECOME_ANY_ACCOUNT mode and
    configuring pull-replication.
    """
    logger.info("Configuring Gerrit for %s…", options.slug)

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

    if options.api_path:
        logger.info("  URL prefix: %s (mirroring production server)", options.api_path)
    else:
        logger.info("  URL prefix: (none)")

    # Core settings
    _gc("gerrit.instanceId", options.slug)
    _gc("gerrit.canonicalWebUrl", options.canonical_url)
    _gc("httpd.listenUrl", options.listen_url)
    _gc("sshd.listenAddress", "*:29418")
    _gc("sshd.advertisedAddress", options.advertised_ssh_addr)

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
    ootb_redirect_url = f"{options.api_path}/login/%23%2F?account_id=1000000"
    _gc("httpd.firstTimeRedirectUrl", ootb_redirect_url)

    _configure_remote_admin(_gc, options)

    _gc("container.user", "gerrit")
    _gc("plugin.pull-replication.enabled", "true")

    logger.info("Gerrit configured ✅")
    logger.info("  Mode: non-replica (web UI enabled)")
    logger.info("  Replication: fetchEvery polling")


def _configure_remote_admin(gc: GitConfig, options: GerritConfigOptions) -> None:
    """Set ``plugins.allowRemoteAdmin`` and warn about the exposure.

    Remote plugin admin is disabled whenever the instance is reachable
    through a tunnel, because the instance authenticates with
    DEVELOPMENT_BECOME_ANY_ACCOUNT.
    """
    if not options.use_tunnel:
        gc("plugins.allowRemoteAdmin", "true")
        return

    gc("plugins.allowRemoteAdmin", "false")
    if _is_private_tunnel(options.tunnel_host):
        logger.info("Tunnel mode active (private network — remote admin disabled).")
    else:
        logger.warning(
            "⚠️  Tunnel mode active with DEVELOPMENT_BECOME_ANY_ACCOUNT auth."
        )
        logger.warning("   Anyone with network access can authenticate as any user.")
        logger.warning("   Remote plugin admin has been disabled to limit exposure.")


def _is_private_tunnel(tunnel_host: str) -> bool:
    """Check whether the tunnel host is a private or VPN address.

    Returns ``True`` when *tunnel_host* is an IPv4 address inside one of
    the RFC 1918 private ranges (``10.0.0.0/8``, ``172.16.0.0/12``,
    ``192.168.0.0/16``) or the CGNAT range (``100.64.0.0/10``, which
    includes Tailscale addresses).  Hostnames that cannot be parsed as
    an IP address (e.g. ``bore.pub``) are treated as public and return
    ``False``.
    """
    if not tunnel_host:
        return False

    try:
        addr = ipaddress.ip_address(tunnel_host)
    except ValueError:
        return False

    if not isinstance(addr, ipaddress.IPv4Address):
        return False

    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
    )
    return any(addr in net for net in private_networks)


def generate_secure_config(
    config_file: Path,
    slug: str,
    config: ActionConfig,
) -> None:
    """Generate ``secure.config`` with authentication credentials.

    Emits a per-remote section for every remote that
    ``generate_replication_config`` writes into the matching
    ``replication.config``.  When ``replicate_meta_refs`` is enabled
    that includes the magic-repo remote (``<slug>-meta``) targeting
    ``All-Users`` and ``All-Projects`` — without explicit credentials
    here the plugin falls back to anonymous auth, which the source
    Gerrit refuses with ``TransportException: not authorized``.
    """
    auth_type = config.auth_type.lower()

    if auth_type == "http_basic":
        sections = [
            f'[remote "{slug}"]\n'
            f"  username = {config.http_username}\n"
            f"  password = {config.http_password}\n"
        ]
        if config.replicate_meta_refs:
            # Mirror the credentials onto the magic-repo remote so
            # the matching ``[remote "<slug>-meta"]`` section emitted
            # by generate_replication_config can authenticate against
            # the source Gerrit when fetching All-Users / All-Projects.
            sections.append(
                f'[remote "{slug}-meta"]\n'
                f"  username = {config.http_username}\n"
                f"  password = {config.http_password}\n"
            )
        content = "".join(sections)
    elif auth_type == "bearer_token":
        # Bearer-token auth applies globally to all remotes via the
        # ``[auth]`` section, so no per-remote duplication is needed
        # for the magic-repo remote.
        content = f"[auth]\n  bearerToken = {config.bearer_token}\n"
    else:
        # SSH auth needs no credentials in secure.config, so the body
        # is empty.  The file itself is still created (empty, 0600)
        # below — only its credential contents are unnecessary here.
        content = ""

    config_file.parent.mkdir(parents=True, exist_ok=True)
    # Create (or truncate) secure.config with 0600 *before* writing
    # any credentials into it, so there is no transient window where
    # the file exists with a more permissive umask-derived mode
    # (e.g. 0644) while it already holds the HTTP password / bearer
    # token.  touch() creates an empty file; the explicit chmod also
    # tightens a pre-existing file (O_CREAT does not relax/alter the
    # mode of an existing file), and write_text() preserves the mode
    # of the now-existing file.
    config_file.touch(mode=0o600, exist_ok=True)
    config_file.chmod(0o600)
    config_file.write_text(content, encoding="utf-8")
