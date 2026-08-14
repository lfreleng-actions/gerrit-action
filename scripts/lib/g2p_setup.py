# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""G2P setup: generate config files and configure containers.

This module is the public façade for the G2P setup machinery.  It
owns the orchestration — :func:`setup_g2p` and :func:`setup_g2p_ssh`
— and re-exports the pieces implemented by its sibling modules so
callers have a single import site:

- :mod:`g2p_paths` — in-container filesystem layout
- :mod:`g2p_ini` — ``gerrit_to_platform.ini`` / replication rendering
- :mod:`g2p_container_io` — writing files into a running container
- :mod:`g2p_hook_wrapper` — the POSIX-shell hook wrapper template
- :mod:`g2p_setup_steps` — the individual idempotent setup steps
- :mod:`g2p_setup_ssh` — SSH key material and client configuration
- :mod:`g2p_selftest` — post-setup plumbing validation

Together they translate a :class:`G2PConfig` into the files and
wrappers required inside a running Gerrit container for
``gerrit_to_platform`` to operate:

- ``gerrit_to_platform.ini`` — app config with token and mappings
- ``replication.config`` symlink — platform detection data
- Gerrit hook wrapper scripts — connect events to g2p console scripts
  and tee every invocation to ``/var/gerrit/logs/g2p-hooks.log`` for
  operator observability
- SSH configuration — keypair and ``known_hosts`` for github.com

Usage::

    from g2p_config import G2PConfig
    from g2p_setup import setup_g2p

    config = G2PConfig.from_environment()
    result = setup_g2p(config, docker, container_id)
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from docker_manager import DockerManager
from errors import G2PSetupError
from g2p_config import G2PConfig
from g2p_container_io import append_file_in_container as _append_file_in_container
from g2p_container_io import write_file_in_container as _write_file_in_container
from g2p_ini import generate_g2p_ini, generate_g2p_replication_section
from g2p_paths import (
    G2P_CONFIG_DIR,
    G2P_HOOK_LOG,
    G2P_INI_PATH,
    G2P_REPLICATION_SYMLINK,
    GERRIT_ETC_DIR,
    GERRIT_HOME,
    GERRIT_HOOKS_DIR,
    GERRIT_LOGS_DIR,
    GERRIT_PLUGINS_DIR,
    GERRIT_REPLICATION_CONFIG,
    GERRIT_TOOLS_VENV_BIN,
    GERRIT_USER_HOME,
    SSH_DIR,
)
from g2p_selftest import (
    G2PSelfTestCheck,
    G2PSelfTestReport,
    selftest_g2p_plumbing,
)
from g2p_setup_ssh import (
    GITHUB_HOST_KEY_ED25519,
    deploy_private_key,
    ensure_ssh_dir,
    fetch_github_host_keys,
    generate_ssh_keypair,
    github_in_known_hosts,
    install_known_hosts,
    install_ssh_client_config,
)
from g2p_setup_steps import (
    setup_g2p_config_dir,
    setup_g2p_hooks,
    setup_g2p_ini,
    setup_g2p_replication_remote,
    setup_g2p_replication_symlink,
)

logger = logging.getLogger(__name__)

# Names re-exported for callers (and test mock targets) that treat
# ``g2p_setup`` as the single entry point for the G2P setup layer.
__all__ = [
    "G2P_CONFIG_DIR",
    "G2P_HOOK_LOG",
    "G2P_INI_PATH",
    "G2P_REPLICATION_SYMLINK",
    "GERRIT_ETC_DIR",
    "GERRIT_HOME",
    "GERRIT_HOOKS_DIR",
    "GERRIT_LOGS_DIR",
    "GERRIT_PLUGINS_DIR",
    "GERRIT_REPLICATION_CONFIG",
    "GERRIT_TOOLS_VENV_BIN",
    "GERRIT_USER_HOME",
    "GITHUB_HOST_KEY_ED25519",
    "SSH_DIR",
    "G2PSelfTestCheck",
    "G2PSelfTestReport",
    "G2PSetupResult",
    "_append_file_in_container",
    "_write_file_in_container",
    "fetch_github_host_keys",
    "generate_g2p_ini",
    "generate_g2p_replication_section",
    "generate_ssh_keypair",
    "selftest_g2p_plumbing",
    "setup_g2p",
    "setup_g2p_config_dir",
    "setup_g2p_hooks",
    "setup_g2p_ini",
    "setup_g2p_replication_remote",
    "setup_g2p_replication_symlink",
    "setup_g2p_ssh",
]


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class G2PSetupResult:
    """Captures the outcome of a G2P setup run for a single container.

    Attributes:
        config_path: Path to the generated INI inside the container.
        hooks_enabled: Hook names whose wrapper scripts the action
            installed (one POSIX-shell wrapper per hook under
            ``/var/gerrit/hooks/``; each invokes the matching
            console script and tees output to
            ``/var/gerrit/logs/g2p-hooks.log``).
        ssh_public_key: Public key (for downstream deploy-key setup).
        ssh_private_key:
            The Ed25519 SSH private key generated for the Gerrit
            container's *outbound* connection to ``github.com``
            (i.e. ``~/.ssh/g2p_github_key`` inside the container).
            This is the key the Gerrit-side push-replication
            plugin authenticates with when talking to GitHub.

            **It is NOT the credential that GitHub Actions
            workflows use to SSH back into Gerrit for review
            voting** — that credential is the
            ``GERRIT_SSH_PRIVKEY`` org secret, which is generated
            and authorised against the Gerrit user account in a
            separate step (see ``setup_g2p_ssh``'s
            documentation).  The current org-provisioning code
            reuses this same field for ``GERRIT_SSH_PRIVKEY``
            because the two keypairs happen to be derived from
            the same Ed25519 generator and both ends of the loop
            run inside this action; that mapping is documented in
            ``provision_org_config`` and will be tightened in a
            follow-up that splits Gerrit-auth and GitHub-auth
            keypairs into distinct fields.
        replication_remote_configured: Whether the g2p detection
            remote is present in ``replication.config`` (either
            already existing or newly appended).
    """

    config_path: str = ""
    hooks_enabled: list[str] = field(default_factory=list)
    ssh_public_key: str = ""
    ssh_private_key: str = ""
    replication_remote_configured: bool = False


# ---------------------------------------------------------------------------
# SSH orchestration
# ---------------------------------------------------------------------------


def _derive_public_key(private_key: str) -> str:
    """Derive the public key from *private_key* via ``ssh-keygen -y``.

    Returns an empty string when ``ssh-keygen`` is unavailable or
    rejects the input.  Failure is not fatal: the public key is only
    needed for downstream deploy-key setup, not for the container to
    authenticate.
    """
    try:
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", "/dev/stdin"],
            input=private_key,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        logger.debug("Could not derive public key from private key")
    return ""


def setup_g2p_ssh(
    docker: DockerManager,
    cid: str,
    config: G2PConfig,
) -> tuple[str, str]:
    """Configure SSH for github.com inside the container.

    Handles:

    1. SSH private key — uses provided key or generates a new Ed25519
       keypair.
    2. ``known_hosts`` — appends github.com host keys (provided or
       scanned).
    3. SSH client config — adds a ``Host github.com`` block with
       ``User git``.

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
    tuple[str, str]
        ``(public_key, private_key)`` — the SSH public key (for
        deploy-key setup) and private key (for org-level secret
        provisioning).  Either may be an empty string if no key
        was configured.
    """
    public_key = ""
    private_key = ""
    key_deployed = False

    # Ensure .ssh directory exists with correct permissions
    ensure_ssh_dir(docker, cid)

    # -- Private key -----------------------------------------------------
    key_path = f"{SSH_DIR}/g2p_github_key"
    if config.ssh_private_key:
        private_key = config.ssh_private_key
        deploy_private_key(docker, cid, key_path, private_key)
        key_deployed = True
        logger.info("Deployed provided SSH private key to %s", key_path)

        # Try to derive public key from the private key
        public_key = _derive_public_key(private_key)
    else:
        # Generate a new keypair
        logger.info("No SSH key provided; generating Ed25519 keypair")
        try:
            private_key, public_key = generate_ssh_keypair()
            deploy_private_key(docker, cid, key_path, private_key)
            key_deployed = True
            logger.info("Generated and deployed SSH keypair to %s", key_path)
        except G2PSetupError as exc:
            logger.warning("SSH keypair generation failed: %s", exc)

    # -- Known hosts -----------------------------------------------------
    known_hosts_path = f"{SSH_DIR}/known_hosts"

    # Check if github.com is already in known_hosts
    if not github_in_known_hosts(docker, cid, known_hosts_path):
        host_keys = config.github_known_hosts or fetch_github_host_keys()
        install_known_hosts(docker, cid, known_hosts_path, host_keys)
    else:
        logger.info("github.com already in %s", known_hosts_path)

    # -- SSH client config -----------------------------------------------
    if key_deployed:
        install_ssh_client_config(docker, cid, key_path)
    else:
        logger.info("No SSH key deployed; skipping SSH client config")

    return public_key, private_key


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def setup_g2p(
    config: G2PConfig,
    docker: DockerManager,
    cid: str,
) -> G2PSetupResult:
    """Run the full G2P setup sequence for a single container.

    This is the main entry point called by the ``configure-g2p.py``
    script for each running Gerrit instance.

    Steps:

    1. Create the g2p config directory
    2. Generate and deploy ``gerrit_to_platform.ini``
    3. Append the g2p detection remote to ``replication.config``
    4. Symlink ``replication.config`` into the g2p config dir
    5. Install Gerrit hook wrapper scripts
    6. Configure SSH for github.com

    Parameters
    ----------
    config:
        Validated :class:`G2PConfig`.
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.

    Returns
    -------
    G2PSetupResult
        Summary of the setup operations performed.

    Raises
    ------
    G2PSetupError
        If a critical setup step fails.
    """
    result = G2PSetupResult()

    try:
        # Step 1: Config directory
        setup_g2p_config_dir(docker, cid)

        # Step 2: INI config
        result.config_path = setup_g2p_ini(docker, cid, config)

        # Step 3: Replication remote
        result.replication_remote_configured = setup_g2p_replication_remote(
            docker,
            cid,
            config,
        )

        # Step 4: Replication config symlink
        setup_g2p_replication_symlink(docker, cid)

        # Step 5: Hook wrapper scripts
        result.hooks_enabled = setup_g2p_hooks(docker, cid, config)

        # Step 6: SSH
        result.ssh_public_key, result.ssh_private_key = setup_g2p_ssh(
            docker,
            cid,
            config,
        )

    except G2PSetupError:
        raise
    except Exception as exc:
        raise G2PSetupError(f"G2P setup failed for container {cid}: {exc}") from exc

    logger.info(
        "G2P setup complete for container %s: config=%s, hooks=%s, ssh_key=%s",
        cid,
        result.config_path,
        result.hooks_enabled,
        "provided" if result.ssh_public_key else "none",
    )
    return result
