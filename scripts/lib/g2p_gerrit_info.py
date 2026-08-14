# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Gerrit connection metadata published to the GitHub organisation.

``provision_org_config`` needs to tell GitHub Actions workflows how
to reach the Gerrit instance this action just started: SSH host,
port, user, host keys and the HTTP base URL.  Deriving those values
means reproducing the tunnel/localhost resolution that
``start-instances.py`` applies, which is fiddly enough to deserve
its own module.

Usage::

    from g2p_gerrit_info import build_gerrit_info

    info = build_gerrit_info(instances, setup_results, action_config)
"""

from __future__ import annotations

from typing import Any

from config import ActionConfig
from g2p_setup import G2PSetupResult


def build_gerrit_info(
    instances: dict[str, dict[str, Any]],
    setup_results: list[G2PSetupResult],
    action_config: ActionConfig,
) -> dict[str, str]:
    """Build the ``gerrit_info`` dict for org provisioning.

    Extracts connection metadata from the first running instance
    and the G2P setup results so that ``provision_org_config`` can
    populate org-level secrets and variables.

    The host and port values are derived from the same tunnel /
    localhost logic used by ``start-instances.py`` so they point
    at the *running container*, not the source Gerrit server.

    Parameters
    ----------
    instances:
        Loaded ``instances.json`` data.
    setup_results:
        Results from :func:`setup_g2p` for each container.
    action_config:
        The global :class:`ActionConfig`.

    Returns
    -------
    dict[str, str]
        Keys: ``ssh_private_key``, ``ssh_host``, ``ssh_port``,
        ``ssh_user``, ``known_hosts``, ``http_url``.
    """
    info: dict[str, str] = {}

    # Use first instance for connection metadata
    if instances:
        first_slug = sorted(instances.keys())[0]
        meta = instances[first_slug]

        ssh_host, ssh_port, http_port = _resolve_endpoint(
            action_config,
            first_slug,
            meta,
        )

        info["ssh_host"] = ssh_host
        info["ssh_port"] = ssh_port
        info["ssh_user"] = action_config.ssh_auth_username or "admin"
        info["http_url"] = _build_http_url(action_config, meta, ssh_host, http_port)
        info["known_hosts"] = _build_known_hosts(meta, ssh_host, ssh_port)

    # SSH private key from the first setup result
    for r in setup_results:
        if r.ssh_private_key:
            info["ssh_private_key"] = r.ssh_private_key
            break

    return info


def _resolve_endpoint(
    action_config: ActionConfig,
    slug: str,
    meta: dict[str, Any],
) -> tuple[str, str, str]:
    """Resolve the effective ``(ssh_host, ssh_port, http_port)``.

    Uses the same logic as ``_resolve_tunnel()`` in
    ``start-instances.py``: tunnel host + tunnel ports when
    configured, otherwise localhost + the container's mapped ports.
    """
    tunnel_host = action_config.tunnel_host
    tunnel_ports = action_config.tunnel_ports
    tc = tunnel_ports.get(slug) if tunnel_host else None

    if tunnel_host and tc:
        return tunnel_host, str(tc.ssh_port), str(tc.http_port)
    return "localhost", str(meta.get("ssh_port", "")), str(meta.get("http_port", ""))


def _build_http_url(
    action_config: ActionConfig,
    meta: dict[str, Any],
    ssh_host: str,
    http_port: str,
) -> str:
    """Construct the HTTP base URL from the effective host/port.

    Optionally appends the instance's API path when ``USE_API_PATH``
    is enabled.
    """
    api_path = meta.get("api_path", "")
    if action_config.use_api_path and api_path:
        # Normalise: ensure leading /, strip trailing /
        if not api_path.startswith("/"):
            api_path = f"/{api_path}"
        api_path = api_path.rstrip("/")
        return f"http://{ssh_host}:{http_port}{api_path}/"
    return f"http://{ssh_host}:{http_port}/"


def _build_known_hosts(
    meta: dict[str, Any],
    ssh_host: str,
    ssh_port: str,
) -> str:
    """Build known_hosts lines from the captured SSH host keys.

    When the SSH endpoint uses a non-standard port, known_hosts
    entries must use the bracketed ``[host]:port`` form so the host
    key is matched.
    """
    host_keys = meta.get("ssh_host_keys", {})
    if ssh_port and ssh_port not in ("", "22"):
        kh_host = f"[{ssh_host}]:{ssh_port}"
    else:
        kh_host = ssh_host
    kh_lines: list[str] = []
    for _key_type, key_data in sorted(host_keys.items()):
        if key_data and ssh_host:
            kh_lines.append(f"{kh_host} {key_data}")
    return "\n".join(kh_lines)
