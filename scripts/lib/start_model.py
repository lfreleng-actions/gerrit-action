# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Value types and constants shared by the instance start-up steps.

Split out of ``start-instances.py`` so the individual step modules can
share the container layout constants, the plugin download URLs and the
option records that keep the step signatures short.  Every name here is
re-exported from ``start-instances.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import ActionConfig, ApiPathStore, InstanceStore

# ---------------------------------------------------------------------------
# Container layout
# ---------------------------------------------------------------------------

# Gerrit container runs as UID:GID 1000:1000
_GERRIT_UID = 1000
_GERRIT_GID = 1000

# Sub-directories mounted into the container under /var/gerrit/
_GERRIT_SUBDIRS = (
    "git",
    "cache",
    "index",
    "data",
    "etc",
    "logs",
    "plugins",
    "tmp",
)

# Plugin download URLs (primary and fallback)
_PLUGIN_URL_TEMPLATE = (
    "https://gerrit-ci.gerritforge.com/job/"
    "plugin-pull-replication-gh-bazel-{version}/"
    "lastSuccessfulBuild/artifact/"
    "bazel-bin/plugins/pull-replication/pull-replication.jar"
)
_PLUGIN_ALT_URL_TEMPLATE = (
    "https://github.com/GerritForge/pull-replication/releases/"
    "download/{version}/pull-replication.jar"
)

# Plugin cache directory
_PLUGIN_CACHE_DIR = Path("/tmp/gerrit-plugins")

# Gerrit API responses carry this XSSI-protection prefix
_XSSI_PREFIX = ")]}'\n"


# ---------------------------------------------------------------------------
# Option records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicationOptions:
    """Per-instance inputs for ``replication.config`` generation."""

    slug: str
    gerrit_host: str
    project: str
    remote_ssh_user: str
    remote_ssh_port: int
    api_path: str
    config: ActionConfig

    @classmethod
    def from_plan(cls, plan: InstancePlan, config: ActionConfig) -> ReplicationOptions:
        """Collect the ``replication.config`` inputs for *plan*."""
        return cls(
            slug=plan.slug,
            gerrit_host=plan.gerrit_host,
            project=plan.project,
            remote_ssh_user=plan.remote_ssh_user,
            remote_ssh_port=plan.remote_ssh_port,
            api_path=plan.api_path,
            config=config,
        )


@dataclass(frozen=True)
class GerritConfigOptions:
    """Per-instance inputs for ``gerrit.config`` generation.

    Deliberately carries no API path of its own.  The path prefix the
    instance serves at is a property of *canonical_url*, which
    ``_build_urls`` has already resolved against ``USE_API_PATH``;
    ``configure_gerrit`` derives it from there so the two cannot
    disagree.
    """

    slug: str
    canonical_url: str
    listen_url: str
    advertised_ssh_addr: str
    use_tunnel: bool
    tunnel_host: str = ""

    @classmethod
    def from_plan(cls, plan: InstancePlan, config: ActionConfig) -> GerritConfigOptions:
        """Collect the ``gerrit.config`` inputs for *plan*."""
        return cls(
            slug=plan.slug,
            canonical_url=plan.canonical_url,
            listen_url=plan.listen_url,
            advertised_ssh_addr=plan.advertised_ssh_addr,
            use_tunnel=plan.use_tunnel,
            tunnel_host=config.tunnel_host,
        )


@dataclass(frozen=True)
class InstanceStartOptions:
    """Ambient state a single instance start-up needs.

    Groups the run-level collaborators (configuration, the two JSON
    stores and the resolved image) with the instance's position in the
    ``GERRIT_SETUP`` list, which fixes its local port offsets.
    """

    index: int
    config: ActionConfig
    api_path_store: ApiPathStore
    instance_store: InstanceStore
    image: str


@dataclass(frozen=True)
class InstancePlan:
    """Ports, URLs and SSH identity resolved for one instance.

    Computed before any container work starts so the provisioning steps
    all agree on the addresses the instance will be reachable at.
    """

    slug: str
    gerrit_host: str
    project: str
    remote_ssh_user: str
    remote_ssh_port: int
    http_port: int
    ssh_port: int
    api_path: str
    api_url: str
    use_tunnel: bool
    url_host: str
    url_http_port: int
    url_ssh_port: int
    advertised_ssh_addr: str
    canonical_url: str
    listen_url: str
