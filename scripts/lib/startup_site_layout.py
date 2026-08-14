# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""On-disk layout of a Gerrit site directory.

A Gerrit instance is provisioned by creating a fixed set of
sub-directories on the host and bind-mounting each of them into the
container.  This module owns that layout:

- :data:`GERRIT_SUBDIRS` — the sub-directories that make up a site.
- :func:`gerrit_volume_mounts` — the host→container mapping derived
  from them, used identically by ``gerrit init`` and by the real
  container run.
- :func:`chown_tree` — making those directories writable by the
  container's ``gerrit`` user.

The sub-directories are mounted individually rather than mounting
``/var/gerrit`` wholesale, so that ``/var/gerrit/bin`` (and the other
image-provided content) is not shadowed by the host directory.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Gerrit container runs as UID:GID 1000:1000
_GERRIT_UID = 1000
_GERRIT_GID = 1000

# Sub-directories mounted into the container under /var/gerrit/
GERRIT_SUBDIRS = (
    "git",
    "cache",
    "index",
    "data",
    "etc",
    "logs",
    "plugins",
    "tmp",
)


def gerrit_volume_mounts(instance_dir: Path) -> dict[str, str]:
    """Return the host→container mounts for a site's sub-directories."""
    return {str(instance_dir / sub): f"/var/gerrit/{sub}" for sub in GERRIT_SUBDIRS}


def chown_tree(path: Path) -> None:
    """Recursively ``chown`` *path* to the Gerrit UID:GID.

    When ``chown`` succeeds the directory is set to 755 (owner-writable).
    When ``chown`` fails (common on CI runners where the workspace is not
    owned by the build user), the fallback uses ``a+rwX`` so the
    container's gerrit user (UID 1000) can still write to mounted volumes.
    """
    try:
        result = subprocess.run(
            ["chown", "-R", f"{_GERRIT_UID}:{_GERRIT_GID}", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            # Ownership set to Gerrit user; safe to use restrictive perms
            subprocess.run(
                ["chmod", "-R", "755", str(path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:
            # Could not change ownership; ensure writability for all users
            # a+rwX keeps execute bits for directories and already-executable files
            subprocess.run(
                ["chmod", "-R", "a+rwX", str(path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
    except (FileNotFoundError, OSError):
        pass
