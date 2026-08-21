# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Per-instance configuration value objects for gerrit-action.

Owns the immutable descriptions of a *single* Gerrit instance, as opposed
to the action-wide settings held by :class:`config.ActionConfig`:

* :class:`InstanceConfig` — one element of the ``gerrit_setup`` JSON
  array, with action-level defaults already applied.
* :class:`TunnelConfig` — the HTTP/SSH port pair published for an
  instance when tunnelling is in use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from config_values import _normalise_path
from errors import ConfigError

# ---------------------------------------------------------------------------
# Per-instance configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceConfig:
    """Configuration for a single Gerrit instance.

    Typically parsed from one element of the ``gerrit_setup`` JSON array.
    """

    slug: str
    gerrit_host: str
    project: str = ""
    api_path: str = ""
    ssh_user: str = ""
    ssh_port: int = 29418
    max_projects: int = 500

    @property
    def effective_api_path(self) -> str:
        """Resolve *api_path*, respecting the ``USE_API_PATH`` flag.

        When ``USE_API_PATH`` is not ``"true"`` the API path is ignored
        (returns ``""``).  Otherwise the stored path is normalised:

        * A leading ``/`` is ensured.
        * A trailing ``/`` is stripped.
        * The bare ``"/"`` is collapsed to ``""``.
        """
        if os.environ.get("USE_API_PATH", "false").lower() != "true":
            return ""
        return _normalise_path(self.api_path)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        default_ssh_user: str = "gerrit",
        default_ssh_port: int = 29418,
        default_max_projects: int = 500,
    ) -> InstanceConfig:
        """Create an :class:`InstanceConfig` from a JSON-decoded dict.

        Missing keys fall back to sensible defaults so that callers do
        not need to specify every field.
        """
        slug = data.get("slug", "")
        if not slug:
            raise ConfigError("Instance config missing required 'slug' field")

        gerrit_host = data.get("gerrit", "")
        if not gerrit_host:
            raise ConfigError(f"Instance '{slug}' missing required 'gerrit' field")

        ssh_user = data.get("ssh_user", "") or default_ssh_user
        raw_ssh_port = data.get("ssh_port", "") or default_ssh_port

        return cls(
            slug=slug,
            gerrit_host=gerrit_host,
            project=data.get("project", ""),
            api_path=data.get("api_path", ""),
            ssh_user=ssh_user,
            ssh_port=int(raw_ssh_port),
            max_projects=int(data.get("max_projects", default_max_projects)),
        )


# ---------------------------------------------------------------------------
# Tunnel configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TunnelConfig:
    """Tunnel port mapping for a single instance."""

    http_port: int
    ssh_port: int
