#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Start Gerrit instances based on JSON configuration.

This script is the main orchestrator for provisioning one or more local
Gerrit containers.  Each provisioning step lives in a sibling module
under ``scripts/lib``; what remains here is the order those steps run
in, the per-instance and run-wide orchestration, and the process exit
codes:

- :mod:`startup_image` — the Docker image instances are started from
- :mod:`startup_endpoints` — local ports, tunnel resolution and URLs
- :mod:`startup_site_layout` — site sub-directories, mounts, ownership
- :mod:`startup_site_init` — bootstrapping a site with ``gerrit init``
- :mod:`startup_gerrit_config` — ``gerrit.config`` settings
- :mod:`startup_plugins` — pull-replication and additional plugin JARs
- :mod:`startup_ssh` — replication SSH auth and host-key capture
- :mod:`startup_source_projects` — the source Gerrit's project list
- :mod:`startup_replication_config` and :mod:`startup_secure_config` —
  ``replication.config`` rendering and its matching credentials
- :mod:`startup_precreate` — bare git repos for ``fetchEvery`` mode
- :mod:`startup_container` — starting the container and recording it
- :mod:`startup_run_context` — collaborators shared across a whole run
- :mod:`startup_summary` — the closing step summary table

Replaces ``start-instances.sh`` (~1,100 lines).

Usage::

    # From action.yaml (via the venv created in the Dockerfile)
    python3 scripts/start-instances.py

    # Locally with environment variables
    WORK_DIR=/tmp/gerrit-action GERRIT_SETUP='[...]' \\
        python3 scripts/start-instances.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup – ensure ``scripts/lib`` is importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from config import (  # noqa: E402
    ActionConfig,
    ApiPathStore,
    InstanceConfig,
    InstanceStore,
)
from docker_manager import DockerManager  # noqa: E402
from errors import GerritActionError  # noqa: E402
from logging_utils import log_group, setup_logging  # noqa: E402
from startup_container import (  # noqa: E402
    build_instance_metadata,
    launch_gerrit_container,
    remove_bundled_replication_plugin,
    report_started_instance,
)
from startup_endpoints import (  # noqa: E402
    InstanceEndpoints,
    is_private_tunnel,
    log_instance_banner,
    resolve_instance_endpoints,
    resolve_tunnel,
    write_env_sh,
)
from startup_gerrit_config import configure_gerrit  # noqa: E402
from startup_image import build_or_reuse_image, verify_custom_image  # noqa: E402
from startup_plugins import (  # noqa: E402
    download_additional_plugins,
    download_file,
    download_plugin,
)
from startup_precreate import (  # noqa: E402
    fetch_and_precreate_projects,
    resolve_project_list,
)
from startup_replication_config import (  # noqa: E402
    ReplicationSource,
    render_replication_config,
)
from startup_run_context import StartupRunContext  # noqa: E402
from startup_secure_config import generate_secure_config  # noqa: E402
from startup_site_init import init_gerrit_site  # noqa: E402
from startup_site_layout import GERRIT_SUBDIRS, chown_tree  # noqa: E402
from startup_ssh import capture_ssh_host_keys, setup_ssh_auth  # noqa: E402
from startup_summary import write_startup_summary  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-exports of helpers that moved into sibling modules
#
# These names are still referenced as attributes of *this* module — by
# the functions below and by callers that import them from here — so
# they keep their original spelling at this level.
#
# ``generate_replication_config`` is now nothing more than the renderer
# itself: the grouping it used to perform is done by its caller, which
# builds the :class:`ReplicationSource` directly.
# ---------------------------------------------------------------------------
_GERRIT_SUBDIRS = GERRIT_SUBDIRS
_chown_tree = chown_tree
_download_file = download_file
_is_private_tunnel = is_private_tunnel
_resolve_project_list = resolve_project_list
_resolve_tunnel = resolve_tunnel
_verify_custom_image = verify_custom_image
_write_env_sh = write_env_sh
_write_startup_summary = write_startup_summary
generate_replication_config = render_replication_config


# =====================================================================
# Docker image management
# =====================================================================


def ensure_custom_image(
    docker: DockerManager,
    config: ActionConfig,
) -> str:
    """Ensure the custom Gerrit image is available and return its tag.

    Resolves the build context — the repository root, which holds the
    ``Dockerfile`` alongside this script's directory — and delegates to
    :func:`startup_image.build_or_reuse_image`.
    """
    return build_or_reuse_image(docker, config, SCRIPT_DIR.parent)


# =====================================================================
# Instance startup orchestrator
# =====================================================================


def _provision_instance_site(
    context: StartupRunContext,
    instance: InstanceConfig,
    endpoints: InstanceEndpoints,
    api_path: str,
) -> int | None:
    """Prepare an instance's site directory, ready for the container.

    Runs the on-disk provisioning steps in order: ``gerrit init``,
    ``gerrit.config``, plugins, replication SSH auth, the replication
    config pair, project pre-creation, and removal of the conflicting
    bundled replication plugin.

    Returns the expected project count, or *None* if the plugin download
    failed and the instance should be abandoned.
    """
    config = context.config
    slug = instance.slug
    gerrit_host = instance.gerrit_host

    # Per-instance SSH settings
    remote_ssh_user = instance.ssh_user or config.remote_ssh_user
    remote_ssh_port = instance.ssh_port or config.remote_ssh_port

    instance_dir = config.work_path / "instances" / slug

    # Step 1: Init site
    init_gerrit_site(
        context.docker,
        instance_dir,
        slug,
        endpoints.canonical_url,
        context.image,
        extra_init_args=config.gerrit_init_args,
    )

    # Step 2: Configure
    configure_gerrit(
        instance_dir, slug, endpoints, api_path, tunnel_host=config.tunnel_host
    )

    # Step 3: Plugins
    if not download_plugin(
        instance_dir / "plugins", config.plugin_version, config.skip_plugin_install
    ):
        return None
    download_additional_plugins(instance_dir / "plugins", config.additional_plugins)

    # Step 4: SSH auth
    if config.auth_type.lower() == "ssh":
        setup_ssh_auth(
            instance_dir,
            gerrit_host,
            remote_ssh_user,
            remote_ssh_port,
            config.ssh_private_key,
            config.ssh_known_hosts,
        )

    # Step 5: Replication config
    generate_replication_config(
        instance_dir / "etc" / "replication.config",
        slug,
        ReplicationSource(
            host=gerrit_host,
            ssh_user=remote_ssh_user,
            ssh_port=remote_ssh_port,
            api_path=api_path,
        ),
        instance.project,
        config,
    )
    generate_secure_config(instance_dir / "etc" / "secure.config", slug, config)

    # Step 6: Pre-create projects
    expected_count = fetch_and_precreate_projects(
        instance_dir, instance, api_path, config
    )

    # Step 7: Remove bundled replication plugin (conflicts with pull-replication)
    remove_bundled_replication_plugin(instance_dir)

    return expected_count


def start_instance(
    context: StartupRunContext,
    instance: InstanceConfig,
    index: int,
) -> bool:
    """Provision and start a single Gerrit container.

    Returns *True* on success, *False* on failure.
    """
    config = context.config
    slug = instance.slug

    # API path from detection phase
    api_path = context.api_path_store.get_api_path(slug)

    endpoints = resolve_instance_endpoints(instance, index, config, api_path)

    # Write env.sh for downstream steps
    _write_env_sh(
        config.work_path,
        endpoints.canonical_url,
        endpoints.listen_url,
        endpoints.advertised_ssh_addr,
        endpoints.use_tunnel,
    )

    log_instance_banner(index, instance, endpoints)

    instance_dir = config.work_path / "instances" / slug

    expected_count = _provision_instance_site(context, instance, endpoints, api_path)
    if expected_count is None:
        return False

    # Step 8: Start container
    container = launch_gerrit_container(
        context.docker, config, instance_dir, slug, endpoints, context.image
    )
    if container is None:
        return False

    # Step 9: Capture SSH host keys
    ssh_host_keys = capture_ssh_host_keys(
        context.docker, container.cid, config.work_path, slug
    )

    # Step 10: Store instance metadata
    metadata = build_instance_metadata(
        instance,
        endpoints,
        container,
        context.api_path_store,
        expected_count,
        ssh_host_keys,
    )
    context.instance_store.set_instance(slug, metadata)
    context.instance_store.save()

    report_started_instance(
        context.docker,
        config,
        container,
        endpoints,
        context.api_path_store.get_api_url(slug),
    )

    return True


# =====================================================================
# Main orchestrator
# =====================================================================


def run() -> int:
    """Start all Gerrit instances defined in ``$GERRIT_SETUP``.

    Reads configuration from environment variables, validates it,
    provisions each instance (init, config, plugins, replication,
    container start), and writes metadata to ``instances.json``.

    Returns
    -------
    int
        Exit code: 0 on success, 1 if any instance failed, 2 on
        unexpected errors.
    """
    config = ActionConfig.from_environment()
    setup_logging(debug=config.debug)

    logger.info("Starting Gerrit instances…")

    # Validate configuration
    errors = config.validate()
    if errors:
        for err in errors:
            logger.error("Configuration error: %s", err)
        return 1

    work_dir = config.work_path
    work_dir.mkdir(parents=True, exist_ok=True)

    # Initialise tracking files
    cid_file = work_dir / "container_ids.txt"
    cid_file.write_text("", encoding="utf-8")

    instance_store = InstanceStore(config.instances_json_path)
    # Start with empty data (don't try to load — file may not exist yet)
    instance_store._data = {}
    instance_store.save()

    # Load API paths from detection phase
    api_path_store = ApiPathStore(config.api_paths_json_path)
    api_path_store.load()

    # Ensure custom Docker image
    docker = DockerManager()

    with log_group("Docker image"):
        image = ensure_custom_image(docker, config)

    context = StartupRunContext(
        docker=docker,
        config=config,
        api_path_store=api_path_store,
        instance_store=instance_store,
        image=image,
    )

    # Start each instance
    failed = 0
    for index, inst in enumerate(config.instances):
        with log_group(f"Instance {index + 1}: {inst.slug}"):
            if not start_instance(context, inst, index):
                logger.error("Failed to start instance %d ❌", index)
                failed += 1

    # Summary
    total = len(config.instances)
    logger.info("========================================")
    if failed == 0:
        logger.info("All instances started! ✅")
    else:
        logger.error("%d of %d instances failed to start ❌", failed, total)
    logger.info("Total instances: %d", total)
    logger.info("========================================")
    logger.info("")

    _write_startup_summary(instance_store)

    return 1 if failed > 0 else 0


def main() -> int:
    """Entry point with structured error handling."""
    try:
        return run()
    except GerritActionError as exc:
        logger.error(str(exc))
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
