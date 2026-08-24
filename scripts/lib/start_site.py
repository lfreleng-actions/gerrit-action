# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Gerrit site layout, ``gerrit init`` and file ownership.

Split out of ``start-instances.py``.  Creates the sub-directories that
are mounted into the container, runs ``gerrit init`` inside the image,
fixes ownership so the container user can write, and appends the
per-instance variables downstream steps read from ``env.sh``.  The
pieces are re-exported from ``start-instances.py``, which keeps the
``init_gerrit_site`` sequence itself.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from docker_manager import DockerManager
from errors import ConfigError, DockerError, GerritActionError
from start_model import _GERRIT_GID, _GERRIT_SUBDIRS, _GERRIT_UID

logger = logging.getLogger(__name__)


def create_site_directories(instance_dir: Path) -> None:
    """Create the Gerrit sub-directories mounted into the container."""
    for subdir in _GERRIT_SUBDIRS:
        d = instance_dir / subdir
        d.mkdir(parents=True, exist_ok=True)


def site_volumes(instance_dir: Path) -> dict[str, str]:
    """Map each site sub-directory to its path inside the container.

    Only the individual sub-directories are mounted (not the whole
    ``/var/gerrit`` directory) so that ``/var/gerrit/bin`` from the
    image is preserved.
    """
    return {str(instance_dir / sub): f"/var/gerrit/{sub}" for sub in _GERRIT_SUBDIRS}


def build_init_command(extra_init_args: str) -> list[str]:
    """Build the ``gerrit init`` argv, honouring *extra_init_args*.

    Raises
    ------
    ConfigError
        If *extra_init_args* cannot be tokenised (e.g. an unbalanced
        quote).
    """
    # Pass --batch so init never prompts, and
    # --install-all-plugins so the bundled plugins from the
    # image (hooks, download-commands, delete-project,
    # webhooks, singleusergroup, reviewnotes, etc.) get copied
    # into the mounted plugins/ directory.  Without this the
    # mount shadows the image's bundled plugins and Gerrit
    # starts without the hooks plugin — which silently breaks
    # G2P because Gerrit never invokes the wrapper scripts in
    # /var/gerrit/hooks/.
    command = ["init", "--batch", "--install-all-plugins"]
    if not extra_init_args.strip():
        return command

    # Honour any user-supplied extras.  Parse with
    # ``shlex.split`` so callers can use the familiar
    # shell-style whitespace-separated form
    # (``--foo bar --baz``) and still get quoting handled
    # correctly for values that contain spaces.  Each
    # token becomes its own argv element, matching the
    # behaviour ``gerrit init`` expects.
    #
    # ``shlex.split`` raises ``ValueError`` on malformed
    # input (e.g. an unbalanced quote).  Convert that to a
    # ConfigError pointing at the offending action input so
    # the user sees an actionable ``::error::`` line
    # instead of an unhelpful stack trace from deep inside
    # the container start path.
    try:
        extra_tokens = shlex.split(extra_init_args)
    except ValueError as exc:
        raise ConfigError(
            f"Invalid 'gerrit_init_args' input ({exc}): {extra_init_args!r}"
        ) from exc
    for extra in extra_tokens:
        if extra:
            command.append(extra)

    return command


def run_gerrit_init(
    docker: DockerManager,
    instance_dir: Path,
    slug: str,
    canonical_url: str,
    image: str,
    extra_init_args: str,
) -> None:
    """Run ``gerrit init`` against the site in *instance_dir*.

    Raises
    ------
    GerritActionError
        If the ephemeral ``gerrit init`` container fails.
    """
    try:
        docker.run_ephemeral(
            image,
            volumes=site_volumes(instance_dir),
            env={"CANONICAL_WEB_URL": canonical_url},
            command=build_init_command(extra_init_args),
            timeout=180,
        )
    except DockerError as exc:
        raise GerritActionError(
            f"Failed to initialize Gerrit site for {slug}: {exc}"
        ) from exc


def _chown_tree(path: Path) -> None:
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


def _write_env_sh(
    work_dir: Path,
    canonical_url: str,
    listen_url: str,
    ssh_addr: str,
    use_tunnel: bool,
) -> None:
    """Append environment variables to ``env.sh`` for downstream steps."""
    env_file = work_dir / "env.sh"
    lines = [
        f"GERRIT_CANONICAL_URL={canonical_url}",
        f"GERRIT_LISTEN_URL={listen_url}",
        f"GERRIT_SSH_ADDR={ssh_addr}",
    ]
    if use_tunnel:
        lines.append("GERRIT_TUNNEL_MODE=true")
    with open(env_file, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"{line}\n")
