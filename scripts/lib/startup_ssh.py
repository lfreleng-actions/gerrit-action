# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""SSH key material for a Gerrit instance.

This module owns both directions of the instance's SSH trust:

- :func:`setup_ssh_auth` writes the *outbound* credentials the
  pull-replication plugin uses to clone from the source Gerrit (private
  key, ``known_hosts``, ``ssh_config``).
- :func:`capture_ssh_host_keys` reads the *inbound* host public keys
  that the started container advertises, so later steps can pin them.

Both deal with the same ``ssh``-shaped state around one instance, and
both are careful about file modes, so they are kept together.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from docker_manager import DockerManager
from errors import DockerError

logger = logging.getLogger(__name__)


def setup_ssh_auth(
    instance_dir: Path,
    gerrit_host: str,
    ssh_user: str,
    ssh_port: int,
    ssh_private_key: str,
    ssh_known_hosts: str,
) -> None:
    """Create the SSH directory structure for replication auth.

    Writes the private key, known_hosts (or fetches via ssh-keyscan),
    and an SSH config file into ``<instance_dir>/ssh/``.
    """
    ssh_dir = instance_dir / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_dir.chmod(0o700)

    # Private key
    id_rsa = ssh_dir / "id_rsa"
    id_rsa.write_text(ssh_private_key, encoding="utf-8")
    id_rsa.chmod(0o600)

    # Known hosts
    known_hosts = ssh_dir / "known_hosts"
    if ssh_known_hosts:
        known_hosts.write_text(ssh_known_hosts, encoding="utf-8")
    else:
        logger.info("Auto-fetching SSH host key for %s:%d…", gerrit_host, ssh_port)
        try:
            result = subprocess.run(
                ["ssh-keyscan", "-H", "-p", str(ssh_port), gerrit_host],
                capture_output=True,
                text=True,
                timeout=30,
            )
            known_hosts.write_text(result.stdout, encoding="utf-8")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning(
                "Could not fetch SSH host key for %s:%d: %s",
                gerrit_host,
                ssh_port,
                exc,
            )
            known_hosts.touch()
    known_hosts.chmod(0o644)

    # SSH config
    ssh_config = ssh_dir / "config"
    ssh_config.write_text(
        f"Host {gerrit_host}\n"
        f"  HostName {gerrit_host}\n"
        f"  User {ssh_user}\n"
        f"  Port {ssh_port}\n"
        f"  IdentityFile /var/gerrit/ssh/id_rsa\n"
        f"  StrictHostKeyChecking yes\n"
        f"  UserKnownHostsFile /var/gerrit/ssh/known_hosts\n",
        encoding="utf-8",
    )
    ssh_config.chmod(0o600)


def capture_ssh_host_keys(
    docker: DockerManager,
    cid: str,
    work_dir: Path,
    slug: str,
) -> dict[str, str]:
    """Capture SSH host *public* keys from a running Gerrit container.

    Returns a mapping of key-file-name (without ``.pub``) to key
    content, e.g.::

        {"ssh_host_ed25519_key": "ssh-ed25519 AAAAC3…"}
    """
    logger.info("Capturing SSH host public keys…")

    keys_dir = work_dir / "ssh_host_keys" / slug
    keys_dir.mkdir(parents=True, exist_ok=True)

    # List public key files inside the container
    try:
        pub_files_raw = docker.exec_cmd(
            cid,
            "ls /var/gerrit/etc/ssh_host_*_key.pub 2>/dev/null",
            timeout=15,
            check=False,
        )
    except DockerError:
        pub_files_raw = ""

    result: dict[str, str] = {}
    for pub_key_path in pub_files_raw.strip().split():
        if not pub_key_path:
            continue
        filename = pub_key_path.rsplit("/", 1)[-1]
        try:
            docker.cp(
                f"{cid}:/var/gerrit/etc/{filename}",
                str(keys_dir / filename),
            )
        except DockerError:
            logger.debug("Could not copy %s from container", filename)
            continue

        local_file = keys_dir / filename
        if local_file.exists():
            key_name = filename.replace(".pub", "")
            content = local_file.read_text(encoding="utf-8").strip()
            result[key_name] = content

    logger.info("  SSH host keys captured ✅")
    return result
