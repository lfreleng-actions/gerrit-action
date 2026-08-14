# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Gerrit endpoint URL construction for health checks.

An instance may be published behind an upstream API path prefix (for
example ``/r``).  Whether that prefix is applied when talking to the
container is a *policy* decision driven by the ``USE_API_PATH`` action
input, and the shell scripts this package replaced re-derived it
inline in several places — which is precisely how those copies drifted
apart.  Both the container probes and the health-check flows resolve
it through this module so they cannot disagree.
"""

from __future__ import annotations

from typing import Any

# Gerrit's HTTP listener *inside* the container. Host port mappings
# differ per instance, but health checks connect to the container IP
# directly and therefore always use the container-side port.
CONTAINER_HTTP_PORT = 8080


def resolve_api_path(instance: dict[str, Any], *, use_api_path: bool) -> str:
    """Return the API path prefix to apply for *instance*.

    Args:
        instance: Instance metadata dict (from ``instances.json``).
        use_api_path: Whether the ``USE_API_PATH`` policy is enabled.

    Returns:
        The instance's ``api_path`` when the policy is enabled and a
        path is configured, otherwise an empty string.
    """
    api_path = instance.get("api_path", "")
    if use_api_path and api_path:
        return str(api_path)
    return ""


def endpoint_url(container_ip: str, api_path: str, path: str) -> str:
    """Build the URL of a Gerrit endpoint on a container.

    Args:
        container_ip: Container IP address.
        api_path: Effective API path prefix; empty when unused.
        path: Endpoint path, including its leading slash.

    Returns:
        The full ``http://`` URL for the endpoint.
    """
    return f"http://{container_ip}:{CONTAINER_HTTP_PORT}{api_path}{path}"
