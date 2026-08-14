# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Deciding how a Gerrit instance is addressed, locally and externally.

An instance always listens on a locally-mapped host port pair, but the
URLs it advertises (``canonicalWebUrl``, the SSH address, the
``env.sh`` values consumed by later steps) may instead point at an
external tunnel.  This module owns that decision and the value type
that carries its result:

- :func:`resolve_tunnel` — is a tunnel configured for this slug?
- :func:`resolve_instance_endpoints` — the full local/advertised
  picture, returned as :class:`InstanceEndpoints`.
- :func:`is_private_tunnel` — whether a tunnel address is on a private
  or VPN network, which decides how loudly we warn about the
  development-mode auth exposed through it.
- :func:`log_instance_banner` — the startup banner reporting the above.
- :func:`write_env_sh` — handing the resolved values to later steps.

Keeping the port/URL arithmetic here means the startup orchestrator
never has to reason about tunnel fallbacks inline.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path

from config import ActionConfig, InstanceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstanceEndpoints:
    """Where an instance listens locally and how it is advertised.

    Attributes
    ----------
    local_http_port:
        Host port mapped to the container's HTTP port.
    local_ssh_port:
        Host port mapped to the container's SSH port.
    use_tunnel:
        *True* when an external tunnel is advertised instead of
        ``localhost``.
    advertised_host:
        Hostname used in the advertised URLs.
    advertised_http_port:
        HTTP port used in the advertised URLs.
    advertised_ssh_port:
        SSH port used in the advertised URLs.
    canonical_url:
        Value for Gerrit's ``gerrit.canonicalWebUrl``.
    listen_url:
        Value for Gerrit's ``httpd.listenUrl`` (container-side).
    advertised_ssh_addr:
        Value for Gerrit's ``sshd.advertisedAddress``.
    """

    local_http_port: int
    local_ssh_port: int
    use_tunnel: bool
    advertised_host: str
    advertised_http_port: int
    advertised_ssh_port: int
    canonical_url: str
    listen_url: str
    advertised_ssh_addr: str


def is_private_tunnel(tunnel_host: str) -> bool:
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


def resolve_tunnel(
    slug: str,
    config: ActionConfig,
) -> tuple[bool, str, int, int]:
    """Determine tunnel configuration for an instance.

    Returns ``(use_tunnel, url_host, url_http_port, url_ssh_port)``.
    The ports returned are the *external* ports to advertise (either
    tunnel ports or the local mapped ports — the caller decides local
    ports separately).
    """
    tunnel_ports = config.tunnel_ports
    tunnel_host = config.tunnel_host

    if tunnel_host and slug in tunnel_ports:
        tc = tunnel_ports[slug]
        logger.info("  External tunnel configured: %s", tunnel_host)
        logger.info("    HTTP port: %d", tc.http_port)
        logger.info("    SSH port: %d", tc.ssh_port)
        return True, tunnel_host, tc.http_port, tc.ssh_port

    if tunnel_host:
        logger.info("  TUNNEL_HOST set but no ports found for slug '%s'", slug)
        logger.info("  Falling back to localhost URLs")

    return False, "localhost", 0, 0  # 0 → caller fills in local ports


def resolve_instance_endpoints(
    instance: InstanceConfig,
    index: int,
    config: ActionConfig,
    api_path: str,
) -> InstanceEndpoints:
    """Resolve local ports, tunnel usage and advertised URLs.

    Parameters
    ----------
    instance:
        The instance being provisioned.
    index:
        Zero-based position of the instance, used to offset the base
        host ports so multiple instances do not collide.
    config:
        Action config (base ports and tunnel settings).
    api_path:
        API path detected on the *source* server; only used to explain
        in the log why it is being ignored when ``USE_API_PATH`` is
        false.
    """
    # Local ports
    http_port = config.base_http_port + index
    ssh_port = config.base_ssh_port + index

    # Effective API path (only used when USE_API_PATH=true)
    effective_api_path = instance.effective_api_path

    # Tunnel configuration
    use_tunnel, url_host, tunnel_http, tunnel_ssh = resolve_tunnel(
        instance.slug, config
    )
    if use_tunnel:
        url_http_port = tunnel_http
        url_ssh_port = tunnel_ssh
    else:
        url_host = "localhost"
        url_http_port = http_port
        url_ssh_port = ssh_port

    advertised_ssh_addr = f"{url_host}:{url_ssh_port}"

    # Build URLs
    if effective_api_path:
        canonical_url = f"http://{url_host}:{url_http_port}{effective_api_path}/"
        listen_url = f"http://*:8080{effective_api_path}/"
        logger.info("  Using API path: %s (USE_API_PATH=true)", effective_api_path)
    else:
        canonical_url = f"http://{url_host}:{url_http_port}/"
        listen_url = "http://*:8080/"
        if api_path:
            logger.info("  API path detected (%s) but USE_API_PATH is false", api_path)
            logger.info("  Serving at root instead")

    return InstanceEndpoints(
        local_http_port=http_port,
        local_ssh_port=ssh_port,
        use_tunnel=use_tunnel,
        advertised_host=url_host,
        advertised_http_port=url_http_port,
        advertised_ssh_port=url_ssh_port,
        canonical_url=canonical_url,
        listen_url=listen_url,
        advertised_ssh_addr=advertised_ssh_addr,
    )


def log_instance_banner(
    index: int,
    instance: InstanceConfig,
    endpoints: InstanceEndpoints,
) -> None:
    """Log the per-instance startup banner."""
    logger.info("")
    logger.info("========================================")
    logger.info("Instance %d: %s", index + 1, instance.slug)
    logger.info("  Project: %s", instance.project or "(all)")
    logger.info("  Source: %s", instance.gerrit_host)
    logger.info("  Local HTTP Port: %d", endpoints.local_http_port)
    logger.info("  Local SSH Port: %d", endpoints.local_ssh_port)
    if endpoints.use_tunnel:
        logger.info("  Tunnel Mode: ENABLED")
        logger.info("  Public URL: %s", endpoints.canonical_url)
        logger.info("  Public SSH: %s", endpoints.advertised_ssh_addr)
    else:
        logger.info("  Tunnel Mode: disabled (localhost)")
    logger.info("========================================")


def write_env_sh(
    work_dir: Path,
    canonical_url: str,
    listen_url: str,
    ssh_addr: str,
    use_tunnel: bool,
) -> None:
    """Append environment variables to ``env.sh`` for downstream steps."""
    env_file = work_dir / "env.sh"
    lines = [
        f"GERRIT_CANONICAL_URL={canonical_url}",
        f"GERRIT_LISTEN_URL={listen_url}",
        f"GERRIT_SSH_ADDR={ssh_addr}",
    ]
    if use_tunnel:
        lines.append("GERRIT_TUNNEL_MODE=true")
    with open(env_file, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"{line}\n")
