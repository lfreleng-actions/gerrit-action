# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""SSH key material and in-container SSH client configuration.

Everything the Gerrit container needs in order to authenticate its
*outbound* connection to ``github.com`` lives here: generating or
deriving key material on the runner, discovering GitHub's host keys,
and installing ``~/.ssh/{g2p_github_key,known_hosts,config}`` inside
the container.  ``g2p_setup.setup_g2p_ssh`` sequences these calls.

Usage::

    from g2p_setup_ssh import ensure_ssh_dir, install_known_hosts

    ensure_ssh_dir(docker, cid)
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from docker_manager import DockerManager
from errors import G2PSetupError
from g2p_container_io import append_file_in_container, write_file_in_container
from g2p_paths import SSH_DIR

logger = logging.getLogger(__name__)

# Well-known GitHub Ed25519 host key (fallback).
GITHUB_HOST_KEY_ED25519 = (
    "github.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)


# ---------------------------------------------------------------------------
# Key material (runner-side)
# ---------------------------------------------------------------------------


def generate_ssh_keypair() -> tuple[str, str]:
    """Generate an Ed25519 SSH keypair for g2p.

    The keypair is created in a temporary directory and both files are
    read into memory before the directory is cleaned up.

    Returns
    -------
    tuple[str, str]
        ``(private_key, public_key)`` as strings.

    Raises
    ------
    G2PSetupError
        If ``ssh-keygen`` fails.
    """
    with tempfile.TemporaryDirectory(prefix="g2p_ssh_") as tmpdir:
        key_path = os.path.join(tmpdir, "g2p_key")
        try:
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-f",
                    key_path,
                    "-N",
                    "",
                    "-C",
                    "gerrit-action-g2p",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            raise G2PSetupError(f"ssh-keygen failed: {exc.stderr.strip()}") from exc
        except subprocess.TimeoutExpired as exc:
            raise G2PSetupError("ssh-keygen timed out after 30 seconds") from exc
        except FileNotFoundError as exc:
            raise G2PSetupError("ssh-keygen not found on PATH") from exc

        private_key = Path(key_path).read_text(encoding="utf-8")
        public_key = Path(f"{key_path}.pub").read_text(encoding="utf-8")

    return private_key.strip(), public_key.strip()


def _scan_matches_pinned_ed25519(scanned: str) -> bool:
    """Report whether *scanned* carries exactly the pinned Ed25519 key.

    A scan with no Ed25519 line at all is rejected: there would be
    nothing to check the response against.

    Parameters
    ----------
    scanned:
        Raw ``ssh-keyscan`` output.

    Returns
    -------
    bool
        ``True`` when an Ed25519 line is present and every such line
        carries the pinned key material.
    """
    pinned_key = GITHUB_HOST_KEY_ED25519.split()[2]
    seen_ed25519 = False

    for line in scanned.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[1] != "ssh-ed25519":
            continue
        seen_ed25519 = True
        if fields[2] != pinned_key:
            return False

    return seen_ed25519


def fetch_github_host_keys() -> str:
    """Fetch GitHub SSH host keys via ``ssh-keyscan``.

    ``ssh-keyscan`` output is unauthenticated. Anything able to
    influence DNS or the network path could answer with its own key,
    which would then be pinned into the container's ``known_hosts``
    and trusted for every later connection. The scan is therefore only
    accepted when the Ed25519 line it returns matches
    :data:`GITHUB_HOST_KEY_ED25519`; on any mismatch, or when the scan
    fails, the pinned key is used on its own.

    Returns
    -------
    str
        One or more ``known_hosts``-formatted lines.
    """
    try:
        result = subprocess.run(
            ["ssh-keyscan", "-t", "ed25519,rsa", "github.com"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            scanned = result.stdout.strip()
            if _scan_matches_pinned_ed25519(scanned):
                return scanned
            logger.warning(
                "ssh-keyscan returned an Ed25519 host key for github.com that "
                "does not match the pinned key; using the pinned key instead"
            )
            return GITHUB_HOST_KEY_ED25519
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    logger.warning("ssh-keyscan failed; using well-known GitHub Ed25519 host key")
    return GITHUB_HOST_KEY_ED25519


# ---------------------------------------------------------------------------
# In-container SSH files
# ---------------------------------------------------------------------------


def ensure_ssh_dir(docker: DockerManager, cid: str) -> None:
    """Create ``~/.ssh`` with the permissions OpenSSH insists on.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    """
    docker.exec_cmd(cid, f"mkdir -p {SSH_DIR}", user="0")
    docker.exec_cmd(cid, f"chmod 700 {SSH_DIR}", user="0")
    docker.exec_cmd(cid, f"chown gerrit:gerrit {SSH_DIR}", user="0")


def deploy_private_key(
    docker: DockerManager,
    cid: str,
    key_path: str,
    private_key: str,
) -> None:
    """Write *private_key* to *key_path* inside the container as 0600.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    key_path:
        Absolute in-container path for the private key.
    private_key:
        Key text (a trailing newline is added if absent).
    """
    write_file_in_container(
        docker,
        cid,
        key_path,
        private_key + "\n",
        mode="0600",
        owner="gerrit:gerrit",
    )


def github_in_known_hosts(
    docker: DockerManager,
    cid: str,
    known_hosts_path: str,
) -> bool:
    """Return True when *known_hosts_path* already mentions github.com."""
    existing = docker.exec_cmd(
        cid,
        f"grep -q 'github.com' {known_hosts_path} 2>/dev/null && echo found || echo missing",
        check=False,
    )
    return existing.strip() == "found"


def install_known_hosts(
    docker: DockerManager,
    cid: str,
    known_hosts_path: str,
    host_keys: str,
) -> None:
    """Append *host_keys* to ``known_hosts`` and fix its ownership.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    known_hosts_path:
        Absolute in-container path to ``known_hosts``.
    host_keys:
        One or more ``known_hosts``-formatted lines.
    """
    append_file_in_container(
        docker,
        cid,
        known_hosts_path,
        host_keys + "\n",
    )
    docker.exec_cmd(cid, f"chmod 644 {known_hosts_path}", user="0")
    docker.exec_cmd(cid, f"chown gerrit:gerrit {known_hosts_path}", user="0")
    logger.info("Added github.com to %s", known_hosts_path)


def install_ssh_client_config(
    docker: DockerManager,
    cid: str,
    key_path: str,
) -> None:
    """Add a ``Host github.com`` block to the container's SSH config.

    Leaves an existing block untouched so repeated runs cannot stack
    duplicate stanzas.  ``StrictHostKeyChecking yes`` is deliberate:
    the ``known_hosts`` entry is installed first, so an unexpected
    host key must fail the connection rather than be trusted.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    key_path:
        Absolute in-container path of the private key to reference.
    """
    ssh_config_path = f"{SSH_DIR}/config"
    ssh_config_block = (
        "\n"
        "# G2P: GitHub SSH configuration\n"
        "Host github.com\n"
        "  User git\n"
        f"  IdentityFile {key_path}\n"
        "  IdentitiesOnly yes\n"
        "  StrictHostKeyChecking yes\n"
    )

    # Check if there's already a github.com Host block
    existing_config = docker.exec_cmd(
        cid,
        f"grep -q 'Host github.com' {ssh_config_path} 2>/dev/null && echo found || echo missing",
        check=False,
    )
    if existing_config.strip() != "found":
        append_file_in_container(
            docker,
            cid,
            ssh_config_path,
            ssh_config_block,
        )
        docker.exec_cmd(cid, f"chmod 644 {ssh_config_path}", user="0")
        docker.exec_cmd(cid, f"chown gerrit:gerrit {ssh_config_path}", user="0")
        logger.info("Added github.com SSH config to %s", ssh_config_path)
    else:
        logger.info("github.com SSH config already present in %s", ssh_config_path)
