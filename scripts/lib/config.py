# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Configuration parsing and validation for gerrit-action.

Replaces the ``jq`` pipelines and repeated environment variable reads
scattered across the shell scripts with typed, validated dataclasses.

This module owns the *action-wide* configuration: :class:`ActionConfig`,
which aggregates every environment variable the action understands and
validates them as a set.  Supporting pieces live alongside it and are
re-exported here so that ``from config import ...`` keeps working:

* :mod:`config_instances` — per-instance value objects
  (:class:`InstanceConfig`, :class:`TunnelConfig`).
* :mod:`config_stores` — the ``instances.json`` / ``api_paths.json``
  stores (:class:`InstanceStore`, :class:`ApiPathStore`).
* :mod:`config_values` — scalar parsing helpers
  (:func:`parse_interval_to_seconds` and friends).

Usage::

    from config import ActionConfig

    config = ActionConfig.from_environment()
    for instance in config.instances:
        print(instance.slug, instance.effective_api_path)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config_instances import InstanceConfig, TunnelConfig
from config_stores import ApiPathStore, InstanceStore
from config_values import (
    _INTERVAL_RE,
    _is_zero_interval,
    _normalise_path,
    _str_to_bool,
    parse_interval_to_seconds,
)
from errors import ConfigError

logger = logging.getLogger(__name__)

# Names re-exported for the many callers that import them from ``config``.
# The underscore-prefixed entries are listed deliberately: they are
# module-internal by convention but are part of this module's historical
# import surface, so they must keep resolving as ``config.<name>``.
__all__ = [
    "DEFAULT_WORK_DIR",
    "ActionConfig",
    "ApiPathStore",
    "ConfigError",
    "InstanceConfig",
    "InstanceStore",
    "TunnelConfig",
    "_INTERVAL_RE",
    "_is_zero_interval",
    "_normalise_path",
    "_str_to_bool",
    "parse_interval_to_seconds",
]

# Default work directory (matches the shell scripts' convention)
DEFAULT_WORK_DIR = "/tmp/gerrit-action"


# ---------------------------------------------------------------------------
# Global action configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionConfig:
    """Global configuration for the gerrit-action.

    Aggregates environment variables and the ``gerrit_setup`` JSON input
    into a single validated object.
    """

    # Authentication
    auth_type: str = "ssh"
    ssh_private_key: str = ""
    ssh_known_hosts: str = ""
    http_username: str = ""
    http_password: str = ""
    bearer_token: str = ""
    remote_ssh_user: str = "gerrit"
    remote_ssh_port: int = 29418

    # Gerrit image / plugins
    gerrit_version: str = "3.13.1-ubuntu24"
    plugin_version: str = "stable-3.13"
    skip_plugin_install: bool = False
    additional_plugins: str = ""
    gerrit_init_args: str = ""

    # Ports
    base_http_port: int = 18080
    base_ssh_port: int = 29418

    # Replication
    sync_on_startup: bool = True
    sync_refs: str = "+refs/heads/*:refs/heads/*,+refs/tags/*:refs/tags/*"
    replication_threads: int = 4
    replication_timeout: int = 120
    fetch_every: str = "60s"
    require_replication_success: bool = False
    replication_wait_timeout: int = 180

    # NoteDb meta-ref replication.  When true, the action also pulls
    # the refs that hold Gerrit's open changes, account identities,
    # external IDs, groups and sequences so the deployed CI Gerrit can
    # render open changes from the source server.  See
    # ``generate_replication_config`` for the exact refspecs.
    #
    # Off by default because it materially increases initial sync
    # size on instances with large review histories.
    replicate_meta_refs: bool = False

    # Run an online reindex (and cache flush) inside each container
    # after the initial pull-replication sync completes.  Required
    # for the Gerrit UI to actually show changes that landed via
    # replicated ``refs/changes/*`` and account/group refs.
    reindex_after_sync: bool = False

    # Behaviour
    check_service: bool = True
    exit: bool = False
    enable_cache: bool = False
    cache_key_suffix: str = ""
    debug: bool = False
    use_api_path: bool = False
    max_projects: int = 500
    # When true (the default), the project list fetched from the
    # source Gerrit is restricted to projects in state ``ACTIVE``,
    # i.e. ``READ_ONLY`` (archived) and ``HIDDEN`` projects are
    # excluded.  This is implemented at the REST query level via
    # ``?state=ACTIVE`` so the source Gerrit does the filtering and
    # we never enumerate archived projects locally.  Set to false
    # via the ``SKIP_ARCHIVED_PROJECTS=false`` env var (or the
    # equivalent ``skip_archived_projects`` action input) to mirror
    # archived projects too — useful when debugging a specific
    # archived repository, at the cost of a longer replication.
    skip_archived_projects: bool = True

    # Tunnelling
    tunnel_host: str = ""
    tunnel_ports_json: str = ""

    # SSH auth keys (for user account setup)
    ssh_auth_keys: str = ""
    ssh_auth_username: str = ""

    # Working directory
    work_dir: str = DEFAULT_WORK_DIR

    # Instances
    instances: list[InstanceConfig] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def work_path(self) -> Path:
        """Return the working directory as a :class:`Path`."""
        return Path(self.work_dir)

    @property
    def instances_json_path(self) -> Path:
        """Path to the ``instances.json`` file created by ``start-instances``."""
        return self.work_path / "instances.json"

    @property
    def api_paths_json_path(self) -> Path:
        """Path to the ``api_paths.json`` file created by ``detect-api-paths``."""
        return self.work_path / "api_paths.json"

    @property
    def custom_image(self) -> str:
        """Docker image tag for the extended Gerrit image."""
        return f"gerrit-extended:{self.gerrit_version}"

    @property
    def fetch_every_enabled(self) -> bool:
        """Whether ``fetchEvery`` polling is enabled (not zero)."""
        return not _is_zero_interval(self.fetch_every)

    @property
    def fetch_interval_seconds(self) -> int:
        """Parse :attr:`fetch_every` to an integer number of seconds."""
        return parse_interval_to_seconds(self.fetch_every)

    @property
    def tunnel_ports(self) -> dict[str, TunnelConfig]:
        """Parse :attr:`tunnel_ports_json` into a slug → TunnelConfig mapping."""
        if not self.tunnel_ports_json:
            return {}
        try:
            raw = json.loads(self.tunnel_ports_json)
        except json.JSONDecodeError:
            logger.warning("TUNNEL_PORTS is not valid JSON, ignoring")
            return {}

        if not isinstance(raw, dict):
            logger.warning("TUNNEL_PORTS is not a JSON object, ignoring")
            return {}

        result: dict[str, TunnelConfig] = {}
        for slug, ports in raw.items():
            if not isinstance(ports, dict):
                logger.warning("Invalid tunnel port entry for %s, ignoring", slug)
                continue
            http_port = ports.get("http")
            ssh_port = ports.get("ssh")
            if http_port and ssh_port:
                try:
                    tc = TunnelConfig(
                        http_port=int(http_port),
                        ssh_port=int(ssh_port),
                    )
                    if 1 <= tc.http_port <= 65535 and 1 <= tc.ssh_port <= 65535:
                        result[slug] = tc
                    else:
                        logger.warning(
                            "Tunnel ports out of range for %s, ignoring", slug
                        )
                except (ValueError, TypeError):
                    logger.warning("Invalid tunnel port values for %s, ignoring", slug)
        return result

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_environment(cls) -> ActionConfig:
        """Parse configuration from environment variables.

        This is the canonical way to create an :class:`ActionConfig` in a
        GitHub Actions context, where all inputs are exposed as
        environment variables.
        """
        env = os.environ.get

        # Parse the gerrit_setup JSON array
        setup_raw = env("GERRIT_SETUP", "[]")
        try:
            setup_json: list[dict[str, Any]] = json.loads(setup_raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"GERRIT_SETUP is not valid JSON: {exc}") from exc

        if not isinstance(setup_json, list):
            raise ConfigError("GERRIT_SETUP must be a JSON array")

        default_ssh_user = env("REMOTE_SSH_USER", "gerrit")
        default_ssh_port = int(env("REMOTE_SSH_PORT", "29418"))
        default_max_projects = int(env("MAX_PROJECTS", "500"))

        instances = [
            InstanceConfig.from_dict(
                inst,
                default_ssh_user=default_ssh_user,
                default_ssh_port=default_ssh_port,
                default_max_projects=default_max_projects,
            )
            for inst in setup_json
        ]

        work_dir = env("WORK_DIR", DEFAULT_WORK_DIR)

        return cls(
            auth_type=env("AUTH_TYPE", "ssh"),
            ssh_private_key=env("SSH_PRIVATE_KEY", ""),
            ssh_known_hosts=env("SSH_KNOWN_HOSTS", ""),
            http_username=env("HTTP_USERNAME", ""),
            http_password=env("HTTP_PASSWORD", ""),
            bearer_token=env("BEARER_TOKEN", ""),
            remote_ssh_user=default_ssh_user,
            remote_ssh_port=default_ssh_port,
            gerrit_version=env("GERRIT_VERSION", "3.13.1-ubuntu24"),
            plugin_version=env("PLUGIN_VERSION", "stable-3.13"),
            skip_plugin_install=_str_to_bool(env("SKIP_PLUGIN_INSTALL", "false")),
            additional_plugins=env("ADDITIONAL_PLUGINS", ""),
            gerrit_init_args=env("GERRIT_INIT_ARGS", ""),
            base_http_port=int(env("BASE_HTTP_PORT", "18080")),
            base_ssh_port=int(env("BASE_SSH_PORT", "29418")),
            sync_on_startup=_str_to_bool(env("SYNC_ON_STARTUP", "true")),
            sync_refs=env(
                "SYNC_REFS",
                "+refs/heads/*:refs/heads/*,+refs/tags/*:refs/tags/*",
            ),
            replication_threads=int(env("REPLICATION_THREADS", "4")),
            replication_timeout=int(env("REPLICATION_TIMEOUT", "120")),
            fetch_every=env("FETCH_EVERY", "60s"),
            require_replication_success=_str_to_bool(
                env("REQUIRE_REPLICATION_SUCCESS", "false")
            ),
            replication_wait_timeout=int(env("REPLICATION_WAIT_TIMEOUT", "180")),
            replicate_meta_refs=_str_to_bool(env("REPLICATE_META_REFS", "false")),
            reindex_after_sync=_str_to_bool(env("REINDEX_AFTER_SYNC", "false")),
            check_service=_str_to_bool(env("CHECK_SERVICE", "true")),
            exit=_str_to_bool(env("EXIT", "false")),
            enable_cache=_str_to_bool(env("ENABLE_CACHE", "false")),
            cache_key_suffix=env("CACHE_KEY_SUFFIX", ""),
            debug=_str_to_bool(env("DEBUG", "false")),
            use_api_path=_str_to_bool(env("USE_API_PATH", "false")),
            max_projects=default_max_projects,
            skip_archived_projects=_str_to_bool(env("SKIP_ARCHIVED_PROJECTS", "true")),
            tunnel_host=env("TUNNEL_HOST", ""),
            tunnel_ports_json=env("TUNNEL_PORTS", ""),
            ssh_auth_keys=env("SSH_AUTH_KEYS", ""),
            ssh_auth_username=env("SSH_AUTH_USERNAME", ""),
            work_dir=work_dir,
            instances=instances,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of validation error messages (empty if valid).

        This performs the same checks that the ``action.yaml`` setup step
        does in Bash, so that Python entry points can report problems
        before starting Docker containers.
        """
        errors: list[str] = []

        if not self.instances:
            errors.append("gerrit_setup is empty – at least one instance is required")

        # Auth validation
        auth = self.auth_type.lower()
        if auth == "ssh" and not self.ssh_private_key:
            errors.append("ssh_private_key required when auth_type=ssh")
        elif auth == "http_basic":
            if not self.http_username or not self.http_password:
                errors.append(
                    "http_username and http_password required when auth_type=http_basic"
                )
        elif auth == "bearer_token":
            if not self.bearer_token:
                errors.append("bearer_token required when auth_type=bearer_token")
        elif auth not in ("ssh", "http_basic", "bearer_token"):
            errors.append(f"Invalid auth_type: {auth}")

        # Port validation
        if not (1 <= self.base_http_port <= 65535):
            errors.append(f"base_http_port out of range: {self.base_http_port}")
        if not (1 <= self.base_ssh_port <= 65535):
            errors.append(f"base_ssh_port out of range: {self.base_ssh_port}")

        # fetch_every format
        if not _INTERVAL_RE.match(self.fetch_every):
            errors.append(
                f"fetch_every must be a valid interval "
                f"(e.g. '60s', '5m', '1h', or '0' to disable): "
                f"got '{self.fetch_every}'"
            )

        # ssh_auth_username validation
        if self.ssh_auth_username:
            if not re.match(r"^[A-Za-z0-9._-]+$", self.ssh_auth_username):
                errors.append(
                    f"Invalid ssh_auth_username: '{self.ssh_auth_username}' – "
                    "must contain only letters, numbers, dots, underscores, hyphens"
                )
            if len(self.ssh_auth_username) > 64:
                errors.append("ssh_auth_username too long (max 64 characters)")

        return errors
