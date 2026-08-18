# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Container lifecycle for the local replication test harness.

Owns everything to do with the throwaway Gerrit container a scenario
runs against: building (or reusing) the image, materialising the
per-scenario ``etc`` configuration, starting the container with the
right port mappings and mounts, waiting for readiness, and tearing it
all down again.

The harness deliberately uses a distinct port pair per scenario index
so several scenarios can be inspected side by side with ``--keep``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import shutil
import textwrap
import time
from pathlib import Path

from docker_manager import DockerManager
from errors import DockerError
from harness_scenarios import Scenario

logger = logging.getLogger(__name__)

# Base port incremented per scenario to avoid conflicts.
_BASE_HTTP_PORT = 18080
_BASE_SSH_PORT = 39418


@dataclasses.dataclass
class ContainerContext:
    """Tracks a running Gerrit container for a single scenario."""

    cid: str
    name: str
    http_port: int
    ssh_port: int
    work_dir: Path


def build_image(
    docker: DockerManager, gerrit_version: str, *, dockerfile_dir: Path
) -> str:
    """Build (or reuse) the extended Gerrit Docker image.

    Parameters
    ----------
    docker:
        Docker CLI wrapper.
    gerrit_version:
        Gerrit image tag to build against.
    dockerfile_dir:
        Directory expected to contain the ``Dockerfile`` and to serve
        as the build context.  Falls back to the stock upstream image
        when no ``Dockerfile`` is present.

    Returns the image tag.
    """
    tag = f"gerrit-test-local:{gerrit_version}"

    # Check if already built
    try:
        result = docker.run_cmd(
            ["image", "inspect", tag],
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("Reusing existing image %s", tag)
            return tag
    except DockerError as exc:
        logger.debug("Image inspect failed for %s: %s", tag, exc)

    dockerfile = dockerfile_dir / "Dockerfile"
    if not dockerfile.exists():
        # Fall back to stock Gerrit image
        stock = f"gerritcodereview/gerrit:{gerrit_version}"
        logger.info("Dockerfile not found; pulling stock image %s", stock)
        docker.run_cmd(["pull", stock], timeout=120)
        return stock

    logger.info("Building image %s from %s …", tag, dockerfile.parent)
    docker.run_cmd(
        [
            "build",
            "-t",
            tag,
            "--build-arg",
            f"GERRIT_VERSION={gerrit_version}",
            str(dockerfile.parent),
        ],
        timeout=300,
    )
    return tag


def _write_instance_config(
    work_dir: Path,
    scenario: Scenario,
    creds: tuple[str, str],
    fetch_every: str,
) -> Path:
    """Write ``replication.config`` and ``secure.config`` for *scenario*.

    Returns the ``etc`` directory that gets mounted into the container.
    """
    etc_dir = work_dir / "etc"
    etc_dir.mkdir(exist_ok=True)

    # --- replication.config ---
    url_template = f"https://{scenario.gerrit_host}{scenario.api_path}/a/${{name}}.git"
    repl_config = textwrap.dedent(f"""\
        [gerrit]
          replicateOnStartup = true
          autoReload = true
        [replication]
          lockErrorMaxRetries = 5
          maxRetries = 5
          useCGitClient = false
          refsBatchSize = 50
        [remote "{scenario.slug}"]
          url = {url_template}
          fetchEvery = {fetch_every}
          timeout = 600
          connectionTimeout = 600000
          replicationDelay = 0
          replicationRetry = 60
          threads = 4
          createMissingRepositories = true
          replicateHiddenProjects = false
          fetch = +refs/heads/*:refs/heads/*
          fetch = +refs/tags/*:refs/tags/*
          fetch = +refs/changes/*:refs/changes/*
    """)
    (etc_dir / "replication.config").write_text(repl_config)

    # --- secure.config ---
    username, password = creds
    secure_config = textwrap.dedent(f"""\
        [remote "{scenario.slug}"]
          username = {username}
          password = {password}
    """)
    secure_path = etc_dir / "secure.config"
    # Create the file with owner-only permissions before writing the
    # credentials so they are never briefly world-readable on disk. This
    # matches the 0600 mode Gerrit itself enforces on secure.config.
    secure_path.touch(mode=0o600, exist_ok=True)
    secure_path.chmod(0o600)
    secure_path.write_text(secure_config)

    return etc_dir


def start_container(
    docker: DockerManager,
    scenario: Scenario,
    image: str,
    index: int,
    creds: tuple[str, str],
    *,
    fetch_every: str = "15s",
) -> ContainerContext:
    """Start a Gerrit container configured for *scenario*.

    Creates the work directory, writes ``replication.config`` and
    ``secure.config``, and starts the container with proper port
    mappings and volume mounts.
    """
    http_port = _BASE_HTTP_PORT + index
    ssh_port = _BASE_SSH_PORT + index
    container_name = f"gerrit-test-{scenario.slug}-{int(time.time()) % 100000}"

    work_dir = Path(f"/tmp/gerrit-test-{scenario.slug}")
    work_dir.mkdir(parents=True, exist_ok=True)

    etc_dir = _write_instance_config(work_dir, scenario, creds, fetch_every)

    # Ensure the git directory exists
    git_dir = work_dir / "git"
    git_dir.mkdir(exist_ok=True)

    # --- Start container ---
    logger.info(
        "Starting container %s  http=%d ssh=%d …",
        container_name,
        http_port,
        ssh_port,
    )

    cid_raw = docker.run_cmd(
        [
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"{http_port}:8080",
            "-p",
            f"{ssh_port}:29418",
            "-v",
            f"{etc_dir}:/var/gerrit/etc",
            "-v",
            f"{git_dir}:/var/gerrit/git",
            "-e",
            "CANONICAL_WEB_URL=http://localhost:8080/",
            image,
        ],
        timeout=30,
    )
    cid = cid_raw.stdout.strip()
    logger.info("  Container ID: %s", cid[:12])

    return ContainerContext(
        cid=cid,
        name=container_name,
        http_port=http_port,
        ssh_port=ssh_port,
        work_dir=work_dir,
    )


def wait_for_gerrit_ready(docker: DockerManager, cid: str, timeout: int = 120) -> bool:
    """Wait for the Gerrit ``ready`` log message."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            logs = docker.container_logs(cid, tail=200)
            if "Gerrit Code Review" in logs and "ready" in logs.lower():
                return True
        except DockerError as exc:
            logger.debug("Could not read container logs while waiting: %s", exc)
        time.sleep(3)
    return False


def cleanup_container(docker: DockerManager, ctx: ContainerContext) -> None:
    """Stop and remove a test container and its work directory."""
    with contextlib.suppress(DockerError):
        docker.run_cmd(["stop", "-t", "5", ctx.cid], check=False, timeout=15)
    with contextlib.suppress(DockerError):
        docker.run_cmd(["rm", "-f", ctx.cid], check=False, timeout=10)
    # Clean up work directory
    if ctx.work_dir.exists():
        shutil.rmtree(ctx.work_dir, ignore_errors=True)
