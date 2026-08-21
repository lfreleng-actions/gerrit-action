# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Primitives for placing file content inside a running container.

The Gerrit container runs as an unprivileged ``gerrit`` user but
``docker cp`` always lands files owned by ``root``.  Every G2P setup
step therefore needs the same "copy in, then fix ownership and mode
as root" dance.  This module owns that mechanic so the setup steps
can stay declarative.

Usage::

    from g2p_container_io import write_file_in_container

    write_file_in_container(docker, cid, "/var/gerrit/x", "body")
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from docker_manager import DockerManager


def write_file_in_container(
    docker: DockerManager,
    cid: str,
    path: str,
    content: str,
    *,
    mode: str = "0600",
    owner: str = "gerrit:gerrit",
) -> None:
    """Write a file inside a running container.

    Uses ``docker cp`` with a local temp file followed by
    ``docker exec chown/chmod`` to set ownership and permissions.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    path:
        Absolute path inside the container.
    content:
        File content to write.
    mode:
        Octal permission string (e.g. ``"0600"``).
    owner:
        ``user:group`` string for ``chown``.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Ensure parent directory exists (as root — gerrit user may
        # lack permission to create arbitrary parent directories).
        parent = str(Path(path).parent)
        docker.exec_cmd(
            cid,
            f"mkdir -p {parent}",
            check=True,
            user="0",
        )

        # docker cp creates files owned by root regardless of the
        # container's USER directive, so chmod/chown must also run
        # as root to modify the newly copied file.
        docker.cp(tmp_path, f"{cid}:{path}")
        docker.exec_cmd(cid, f"chmod {mode} {path}", user="0")
        docker.exec_cmd(cid, f"chown {owner} {path}", user="0")
    finally:
        os.unlink(tmp_path)


def append_file_in_container(
    docker: DockerManager,
    cid: str,
    path: str,
    content: str,
) -> None:
    """Append content to an existing file inside a container.

    Creates a local temporary file, copies it into the container with
    ``docker cp``, then appends it to *path* using
    ``cat >> … && rm -f`` inside the container.  The temporary files
    (local and in-container) are removed after the operation.

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Container ID or name.
    path:
        Absolute path to the file inside the container.
    content:
        Content to append.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Copy to a temp location in the container, then append.
        # docker cp creates the temp file owned by root (0600), so
        # the cat/rm must run as root to read and clean it up.
        container_tmp = f"/tmp/g2p_append_{uuid.uuid4().hex}.tmp"
        docker.cp(tmp_path, f"{cid}:{container_tmp}")
        docker.exec_cmd(
            cid,
            f"cat {container_tmp} >> {path} && rm -f {container_tmp}",
            user="0",
        )
    finally:
        os.unlink(tmp_path)
