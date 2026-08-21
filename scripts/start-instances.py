#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Start Gerrit instances based on JSON configuration.

This script is the main orchestrator for provisioning one or more local
Gerrit containers.  It handles:

- Docker image management (check/build custom image)
- SSH authentication setup (private key, known_hosts, ssh_config)
- Remote project list fetching (REST API with auth)
- Replication configuration generation (replication.config, secure.config)
- Plugin download (pull-replication, additional plugins)
- Gerrit site initialisation (``gerrit init`` via Docker)
- Gerrit configuration (``gerrit.config`` via ``git config``)
- Project pre-creation (bare git repos for fetchEvery mode)
- Container startup (``docker run`` with volumes, ports, env)
- SSH host key capture from running containers
- Instance metadata persistence (``instances.json``)

Replaces ``start-instances.sh`` (~1,100 lines).

Each step is implemented in a focused module under ``scripts/lib`` and
re-exported here, so this file stays the single entry point:
:mod:`start_model` (constants and option records), :mod:`start_image`,
:mod:`start_ssh`, :mod:`start_projects`, :mod:`start_plugins`,
:mod:`start_site`, :mod:`start_gerrit_config`,
:mod:`start_replication_config` with :mod:`start_replication_remotes`,
and :mod:`start_container`.

The step sequences stay here so that :func:`start_instance` and
:func:`run` resolve every step as an attribute of this module, which is
how callers substitute individual steps.

Usage::

    # From action.yaml (via the venv created in the Dockerfile)
    python3 scripts/start-instances.py

    # Locally with environment variables
    WORK_DIR=/tmp/gerrit-action GERRIT_SETUP='[...]' \\
        python3 scripts/start-instances.py
"""

from __future__ import annotations

# ``subprocess`` and ``requests`` no longer have call sites in this
# module, but the steps that moved out reach those seams through
# ``start_instances.<module>``; the attributes have to keep resolving
# here.
import logging
import subprocess  # noqa: F401
import sys
from pathlib import Path

import requests  # noqa: F401

# ---------------------------------------------------------------------------
# Path setup – ensure ``scripts/lib`` is importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from config import ActionConfig, InstanceConfig, InstanceStore  # noqa: E402
from docker_manager import DockerManager  # noqa: E402
from errors import GerritActionError  # noqa: E402
from logging_utils import log_group, setup_logging  # noqa: E402
from outputs import write_summary  # noqa: E402
from start_container import (  # noqa: E402
    _build_urls,  # noqa: F401
    _resolve_tunnel,  # noqa: F401
    finish_instance,
    format_startup_summary,
    launch_container,
    log_instance_banner,
    log_startup_totals,
    plan_instance_startup,
    prepare_run_state,
)
from start_gerrit_config import (  # noqa: E402
    _is_private_tunnel,  # noqa: F401
    configure_gerrit,
    generate_secure_config,
)
from start_image import (  # noqa: E402
    _verify_custom_image,  # noqa: F401
    resolve_custom_image,
)
from start_model import (  # noqa: E402
    _GERRIT_GID,  # noqa: F401
    _GERRIT_SUBDIRS,  # noqa: F401
    _GERRIT_UID,  # noqa: F401
    _PLUGIN_ALT_URL_TEMPLATE,  # noqa: F401
    _PLUGIN_CACHE_DIR,
    _PLUGIN_URL_TEMPLATE,  # noqa: F401
    _XSSI_PREFIX,  # noqa: F401
    GerritConfigOptions,
    InstanceStartOptions,
    ReplicationOptions,
)
from start_plugins import (  # noqa: E402
    _download_file,
    install_extra_plugins,
    install_pull_replication_plugin,
)
from start_projects import (  # noqa: E402
    fetch_remote_projects,
    precreate_projects,
    resolve_project_list,
)
from start_replication_config import generate_replication_config  # noqa: E402
from start_site import (  # noqa: E402
    _chown_tree,
    _write_env_sh,
    create_site_directories,
    run_gerrit_init,
)
from start_ssh import capture_ssh_host_keys, setup_ssh_auth  # noqa: E402

logger = logging.getLogger(__name__)


# =====================================================================
# Docker image management
# =====================================================================


def ensure_custom_image(
    docker: DockerManager,
    config: ActionConfig,
) -> str:
    """Ensure the custom Gerrit image is available and return its tag.

    Builds from the Dockerfile alongside this script; see
    :func:`start_image.resolve_custom_image`.
    """
    return resolve_custom_image(docker, config, SCRIPT_DIR.parent)


# =====================================================================
# Plugin download
# =====================================================================


def download_plugin(
    plugin_dir: Path,
    plugin_version: str,
    skip_plugin_install: bool,
) -> bool:
    """Download the pull-replication plugin JAR.

    Returns *True* on success, *False* on failure.  See
    :func:`start_plugins.install_pull_replication_plugin`.
    """
    if skip_plugin_install:
        logger.info("Skipping plugin download (skip_plugin_install=true)")
        return True

    return install_pull_replication_plugin(
        plugin_dir, plugin_version, _PLUGIN_CACHE_DIR, _download_file
    )


def download_additional_plugins(
    plugin_dir: Path,
    additional_plugins: str,
) -> None:
    """Download additional plugins from comma-separated URLs."""
    if not additional_plugins:
        return

    install_extra_plugins(plugin_dir, additional_plugins, _download_file)


# =====================================================================
# Gerrit site initialisation
# =====================================================================


def init_gerrit_site(
    docker: DockerManager,
    instance_dir: Path,
    slug: str,
    canonical_url: str,
    image: str,
    extra_init_args: str = "",
) -> None:
    """Initialise a Gerrit site directory using ``gerrit init``.

    Creates and chowns the mounted sub-directories, then runs
    ``gerrit init`` inside *image*.  *extra_init_args* is the
    ``gerrit_init_args`` action input; see
    :func:`start_site.build_init_command` for how it is tokenised.
    """
    logger.info("Initializing Gerrit site for %s…", slug)

    # Create sub-directories with Gerrit-compatible ownership
    create_site_directories(instance_dir)
    _chown_tree(instance_dir)

    run_gerrit_init(docker, instance_dir, slug, canonical_url, image, extra_init_args)

    logger.info("Gerrit site initialized ✅")


# =====================================================================
# Project pre-creation
# =====================================================================


def _resolve_project_list(
    instance: InstanceConfig,
    api_path: str,
    config: ActionConfig,
) -> list[str]:
    """Resolve the list of projects to pre-create.

    See :func:`start_projects.resolve_project_list`.
    """
    return resolve_project_list(instance, api_path, config, fetch_remote_projects)


def fetch_and_precreate_projects(
    instance_dir: Path,
    instance: InstanceConfig,
    api_path: str,
    config: ActionConfig,
) -> int:
    """Fetch expected projects and pre-create bare git repos.

    Pre-creation is **required** because the ``fetchEvery`` mode only
    polls repositories that already exist in Gerrit's ``projectCache``.

    Returns the expected project count (excluding system repos).
    """
    logger.info("Fetching expected project count from remote…")

    raw_projects = _resolve_project_list(instance, api_path, config)

    return precreate_projects(instance_dir, raw_projects, _chown_tree)


# =====================================================================
# Instance startup orchestrator
# =====================================================================


def start_instance(
    docker: DockerManager,
    instance: InstanceConfig,
    options: InstanceStartOptions,
) -> bool:
    """Provision and start a single Gerrit container.

    Returns *True* on success, *False* on failure.
    """
    config = options.config
    plan = plan_instance_startup(instance, options)

    # Write env.sh for downstream steps
    _write_env_sh(
        config.work_path,
        plan.canonical_url,
        plan.listen_url,
        plan.advertised_ssh_addr,
        plan.use_tunnel,
    )

    log_instance_banner(plan, options.index)

    instance_dir = config.work_path / "instances" / plan.slug

    # Step 1: Init site
    init_gerrit_site(
        docker,
        instance_dir,
        plan.slug,
        plan.canonical_url,
        options.image,
        extra_init_args=config.gerrit_init_args,
    )

    # Step 2: Configure
    configure_gerrit(instance_dir, GerritConfigOptions.from_plan(plan, config))

    # Step 3: Plugins
    if not download_plugin(
        instance_dir / "plugins", config.plugin_version, config.skip_plugin_install
    ):
        return False
    download_additional_plugins(instance_dir / "plugins", config.additional_plugins)

    # Step 4: SSH auth
    if config.auth_type.lower() == "ssh":
        setup_ssh_auth(
            instance_dir,
            plan.gerrit_host,
            plan.remote_ssh_user,
            plan.remote_ssh_port,
            config.ssh_private_key,
            config.ssh_known_hosts,
        )

    # Step 5: Replication config
    generate_replication_config(
        instance_dir / "etc" / "replication.config",
        ReplicationOptions.from_plan(plan, config),
    )
    generate_secure_config(
        instance_dir / "etc" / "secure.config",
        plan.slug,
        config,
    )

    # Step 6: Pre-create projects
    expected_count = fetch_and_precreate_projects(
        instance_dir, instance, plan.api_path, config
    )

    # Step 7: Remove bundled replication plugin (conflicts with pull-replication)
    bundled = instance_dir / "plugins" / "replication.jar"
    if bundled.exists():
        bundled.unlink()

    # Step 8: Start container
    started = launch_container(docker, plan, options, instance_dir)
    if started is None:
        return False

    # Step 9: Capture SSH host keys
    ssh_host_keys = capture_ssh_host_keys(
        docker, started.cid, config.work_path, plan.slug
    )

    # Step 10: Store instance metadata
    finish_instance(docker, plan, options, started, expected_count, ssh_host_keys)

    return True


# =====================================================================
# Helpers
# =====================================================================


def _write_startup_summary(instance_store: InstanceStore) -> None:
    """Write the step summary table for started instances."""
    write_summary(format_startup_summary(instance_store))


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

    instance_store, api_path_store = prepare_run_state(config)

    # Ensure custom Docker image
    docker = DockerManager()

    with log_group("Docker image"):
        image = ensure_custom_image(docker, config)

    # Start each instance
    failed = 0
    for index, inst in enumerate(config.instances):
        with log_group(f"Instance {index + 1}: {inst.slug}"):
            ok = start_instance(
                docker,
                inst,
                InstanceStartOptions(
                    index=index,
                    config=config,
                    api_path_store=api_path_store,
                    instance_store=instance_store,
                    image=image,
                ),
            )
            if not ok:
                logger.error("Failed to start instance %d ❌", index)
                failed += 1

    log_startup_totals(failed, len(config.instances))

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
