# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Individual, idempotent G2P setup steps for a Gerrit container.

Each function here performs exactly one stage of the deployment —
creating the config directory, deploying the INI, wiring the
replication config, installing the hook wrappers — and is safe to
re-run against an already-configured container.  ``g2p_setup``
sequences them; keeping them separate makes each stage testable
against a mock :class:`DockerManager` in isolation.

Usage::

    from g2p_setup_steps import setup_g2p_ini

    ini_path = setup_g2p_ini(docker, cid, config)
"""

from __future__ import annotations

import logging

from docker_manager import DockerManager
from g2p_config import G2PConfig
from g2p_container_io import append_file_in_container, write_file_in_container
from g2p_hook_wrapper import build_hook_wrapper
from g2p_ini import generate_g2p_ini, generate_g2p_replication_section
from g2p_paths import (
    G2P_CONFIG_DIR,
    G2P_HOOK_LOG,
    G2P_INI_PATH,
    G2P_REPLICATION_SYMLINK,
    GERRIT_HOOKS_DIR,
    GERRIT_PLUGINS_DIR,
    GERRIT_REPLICATION_CONFIG,
    GERRIT_TOOLS_VENV_BIN,
)

logger = logging.getLogger(__name__)


def setup_g2p_config_dir(
    docker: DockerManager,
    cid: str,
) -> None:
    """Create the g2p config directory inside the container.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    """
    docker.exec_cmd(cid, f"mkdir -p {G2P_CONFIG_DIR}", user="0")
    docker.exec_cmd(cid, f"chown -R gerrit:gerrit {G2P_CONFIG_DIR}", user="0")
    logger.debug("Created g2p config directory: %s", G2P_CONFIG_DIR)


def setup_g2p_ini(
    docker: DockerManager,
    cid: str,
    config: G2PConfig,
) -> str:
    """Generate and deploy ``gerrit_to_platform.ini`` inside a container.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    config:
        Validated :class:`G2PConfig`.

    Returns
    -------
    str
        The container path where the INI was written.
    """
    ini_content = generate_g2p_ini(config)

    write_file_in_container(
        docker,
        cid,
        G2P_INI_PATH,
        ini_content,
        mode="0600",
        owner="gerrit:gerrit",
    )

    logger.info("Wrote g2p config: %s", G2P_INI_PATH)
    return G2P_INI_PATH


def setup_g2p_replication_symlink(
    docker: DockerManager,
    cid: str,
) -> None:
    """Create the replication.config symlink in the g2p config dir.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    """
    docker.exec_cmd(
        cid,
        f"ln -sf {GERRIT_REPLICATION_CONFIG} {G2P_REPLICATION_SYMLINK}",
        user="0",
    )
    docker.exec_cmd(
        cid,
        f"chown -h gerrit:gerrit {G2P_REPLICATION_SYMLINK}",
        user="0",
    )
    logger.info(
        "Symlinked %s -> %s",
        G2P_REPLICATION_SYMLINK,
        GERRIT_REPLICATION_CONFIG,
    )


def setup_g2p_replication_remote(
    docker: DockerManager,
    cid: str,
    config: G2PConfig,
) -> bool:
    """Ensure the g2p platform detection remote is in replication.config.

    Appends the section when absent; leaves it untouched when already
    present.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    config:
        Validated :class:`G2PConfig`.

    Returns
    -------
    bool
        *True* if the remote is configured (already present or newly
        appended), *False* if skipped (e.g. no effective URL).
    """
    section = generate_g2p_replication_section(config)
    if not section:
        logger.warning("No g2p replication section generated (no effective remote URL)")
        return False

    # Check the section doesn't already exist
    existing = docker.exec_cmd(
        cid,
        f"grep -q '^\\[remote \"github-g2p\"\\]' {GERRIT_REPLICATION_CONFIG} 2>/dev/null && echo found || echo missing",
        check=False,
    )
    if existing.strip() == "found":
        logger.info(
            "G2P detection remote already present in %s",
            GERRIT_REPLICATION_CONFIG,
        )
        return True

    append_file_in_container(docker, cid, GERRIT_REPLICATION_CONFIG, section)
    logger.info(
        "Appended g2p platform detection remote to %s",
        GERRIT_REPLICATION_CONFIG,
    )
    return True


def _warn_if_hooks_plugin_missing(docker: DockerManager, cid: str) -> None:
    """Warn when ``hooks.jar`` is absent from the plugins directory.

    Without it Gerrit never invokes the scripts in
    ``/var/gerrit/hooks/``, which would silently break G2P.
    ``gerrit init --install-all-plugins`` is responsible for placing
    hooks.jar; if it is missing here, the hook wrappers installed by
    :func:`setup_g2p_hooks` will be inert.
    """
    if not docker.exec_test(cid, f"-f {GERRIT_PLUGINS_DIR}/hooks.jar"):
        logger.warning(
            "Gerrit 'hooks' plugin (hooks.jar) is missing from %s — "
            "G2P hook scripts will not run.  Ensure the site is "
            "initialised with 'gerrit init --install-all-plugins'.",
            GERRIT_PLUGINS_DIR,
        )


def _install_hook_wrapper(
    docker: DockerManager,
    cid: str,
    hook_name: str,
) -> bool:
    """Install the wrapper for a single hook; report whether it landed.

    Returns ``False`` (and installs nothing) when the underlying
    ``gerrit_to_platform`` console script is absent, since a wrapper
    pointing at a missing target would fail at dispatch time.
    """
    target_bin = f"{GERRIT_TOOLS_VENV_BIN}/{hook_name}"
    hook_path = f"{GERRIT_HOOKS_DIR}/{hook_name}"

    # Verify the target binary exists
    if not docker.exec_test(cid, f"-f {target_bin}"):
        logger.warning(
            "G2P console script not found: %s — skipping hook %s",
            target_bin,
            hook_name,
        )
        return False

    # Install a small POSIX-shell wrapper script.  The wrapper
    # preserves Gerrit's hook contract (same argv passed to the
    # underlying console script, same stdout/stderr forwarded
    # back to the hooks plugin, same exit code) while teeing
    # every invocation to a known log file under
    # /var/gerrit/logs/g2p-hooks.log.  Without this we have zero
    # in-container observability of whether gerrit_to_platform
    # actually fires for a given event — see also the G2P
    # plumbing self-test that exercises the script with --help
    # to prove imports work.
    wrapper = build_hook_wrapper(hook_name, target_bin)
    write_file_in_container(
        docker,
        cid,
        hook_path,
        wrapper,
        mode="0755",
        owner="gerrit:gerrit",
    )
    logger.info(
        "Hook wrapper: %s -> %s (log: %s)",
        hook_path,
        target_bin,
        G2P_HOOK_LOG,
    )
    return True


def setup_g2p_hooks(
    docker: DockerManager,
    cid: str,
    config: G2PConfig,
) -> list[str]:
    """Install Gerrit hook wrappers for each enabled g2p hook.

    For each hook in ``config.hooks`` the function writes a small
    POSIX-shell wrapper script into ``/var/gerrit/hooks/`` (see
    :func:`g2p_hook_wrapper.build_hook_wrapper`).  The wrapper
    preserves the Gerrit hook contract — same argv passed through to
    the underlying ``gerrit_to_platform`` console script, same
    stdout/stderr forwarded back to the hooks plugin, same exit code —
    while teeing every invocation to
    ``/var/gerrit/logs/g2p-hooks.log`` so an operator can confirm
    whether the hook fired and what the underlying script returned.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    config:
        Validated :class:`G2PConfig`.

    Returns
    -------
    list[str]
        Hook names whose wrapper scripts were successfully
        installed.
    """
    enabled: list[str] = []

    # Ensure the hooks directory exists
    docker.exec_cmd(cid, f"mkdir -p {GERRIT_HOOKS_DIR}", user="0")

    _warn_if_hooks_plugin_missing(docker, cid)

    for hook_name in config.hooks:
        if _install_hook_wrapper(docker, cid, hook_name):
            enabled.append(hook_name)

    # Make sure the destination log file exists and is writable
    # by the gerrit user before the first hook fires — the wrapper
    # appends to it, so a missing file is fine, but pre-creating
    # avoids a race during the very first invocation and lets
    # operators ``tail -F`` it from container start.
    docker.exec_cmd(
        cid,
        f"touch {G2P_HOOK_LOG} && chown gerrit:gerrit {G2P_HOOK_LOG} "
        f"&& chmod 0644 {G2P_HOOK_LOG}",
        user="0",
    )

    return enabled
