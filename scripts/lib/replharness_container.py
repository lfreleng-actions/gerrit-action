# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Container lifecycle for the local replication test harness.

Split out of ``scripts/test-replication-local.py``.  Covers credential
resolution, building (or pulling) the Gerrit image, starting a
scenario's container with its generated ``replication.config`` /
``secure.config``, waiting for Gerrit to report ready, pausing for the
first pull-replication cycle, and tearing the container down again.
Every name here is re-exported from the harness entry point.
"""

from __future__ import annotations

import contextlib
import logging
import netrc
import os
import shutil
import signal
import sys
import textwrap
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from config import parse_interval_to_seconds
from docker_manager import DockerManager
from errors import DockerError
from replharness_model import (
    _BASE_HTTP_PORT,
    _BASE_SSH_PORT,
    SCRIPTS_DIR,
    Scenario,
    _ContainerContext,
)

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _deferred_interrupts() -> Iterator[None]:
    """Hold SIGINT/SIGTERM until the block finishes.

    Used around ``docker run`` so the interrupt handler cannot observe
    the registry mid-update: the signal is delivered once the real
    container context has replaced its placeholder, at which point the
    handler has something it can actually remove.  The delay is bounded
    by ``docker run -d``, which returns as soon as the container is
    created rather than waiting for Gerrit to start.

    Falls back to doing nothing where ``pthread_sigmask`` is
    unavailable, since this harness only ever runs on developer
    machines.
    """
    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:
        yield
        return

    blocked = {signal.SIGINT, signal.SIGTERM}
    mask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        mask(signal.SIG_UNBLOCK, blocked)


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def _get_credentials(host: str) -> tuple[str, str]:
    """Resolve HTTP Basic credentials from env vars or ~/.netrc.

    Returns ``(username, password)`` or raises ``SystemExit``.
    """
    user = os.environ.get("GERRIT_HTTP_USERNAME", "").strip()
    password = os.environ.get("GERRIT_HTTP_PASSWORD", "").strip()
    if user and password:
        return user, password

    # Try ~/.netrc
    try:
        nrc = netrc.netrc()
        auth = nrc.authenticators(host)
        if auth and auth[2] is not None:
            return auth[0], auth[2]
    except (FileNotFoundError, netrc.NetrcParseError):
        pass

    logger.error(
        "No credentials found for %s.  Set GERRIT_HTTP_USERNAME / "
        "GERRIT_HTTP_PASSWORD or add an entry to ~/.netrc.",
        host,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------


def _build_image(docker: DockerManager, gerrit_version: str) -> str:
    """Build (or reuse) the extended Gerrit Docker image.

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

    dockerfile = SCRIPTS_DIR.parent / "Dockerfile"
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
    etc_dir: Path,
    scenario: Scenario,
    creds: tuple[str, str],
    fetch_every: str,
) -> None:
    """Write ``replication.config`` and ``secure.config`` for a scenario."""
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


def _start_container(
    docker: DockerManager,
    scenario: Scenario,
    image: str,
    index: int,
    creds: tuple[str, str],
    *,
    fetch_every: str = "15s",
    tracked: list[_ContainerContext] | None = None,
) -> _ContainerContext:
    """Start a Gerrit container configured for *scenario*.

    Creates the work directory, writes ``replication.config`` and
    ``secure.config``, and starts the container with proper port
    mappings and volume mounts.

    *tracked* is the interrupt handler's registry.  A provisional entry
    keyed by container **name** is registered before ``docker run``,
    because that is the window in which an interrupt is most likely to
    leak a container: Docker may have created it while the command has
    not yet returned its ID.  ``docker rm -f`` accepts a name just as
    well as an ID, so the provisional entry is actionable.  Interrupts
    are held for the duration of the run so the handler never sees the
    registry mid-update, and a run that fails or is interrupted is
    cleaned up by name before the placeholder is dropped.
    """
    http_port = _BASE_HTTP_PORT + index
    ssh_port = _BASE_SSH_PORT + index
    # One token identifies this run, and both the container and its
    # work directory carry it.  The previous scheme gave the container
    # a one-second-resolution suffix (wrapping modulo 100000) and gave
    # every run of a scenario the *same* work directory, so two
    # concurrent runs of one scenario shared their generated configs
    # and could collide on the container name.  That matters now the
    # start-up failure path cleans up by name: without a unique token
    # it could tear down a container, and delete the configs, of a run
    # it does not own.  The pid keeps the name legible to a human
    # inspecting ``docker ps``; the random suffix makes collisions
    # improbable even within one process.
    run_token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    container_name = f"gerrit-test-{scenario.slug}-{run_token}"

    work_dir = Path(f"/tmp/gerrit-test-{scenario.slug}-{run_token}")

    # Register before touching the filesystem, not merely before
    # ``docker run``.  The preparation below writes credentials into
    # ``secure.config``, and an interrupt part-way through would
    # otherwise leave that password in ``/tmp`` with nothing tracking
    # it — and, because the run token makes the path unique, no later
    # run would ever reuse or clean it.  The context is actionable
    # from this point: ``_cleanup_container`` removes the directory,
    # and ``docker rm -f`` on a container that does not exist yet is a
    # harmless no-op.
    provisional = _ContainerContext(
        cid=container_name,
        name=container_name,
        http_port=http_port,
        ssh_port=ssh_port,
        work_dir=work_dir,
    )
    if tracked is not None:
        tracked.append(provisional)

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        etc_dir = work_dir / "etc"
        etc_dir.mkdir(exist_ok=True)
        _write_instance_config(etc_dir, scenario, creds, fetch_every)

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

        with _deferred_interrupts():
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
            ctx = _ContainerContext(
                cid=cid,
                name=container_name,
                http_port=http_port,
                ssh_port=ssh_port,
                work_dir=work_dir,
            )
            if tracked is not None:
                # Register the real entry *before* dropping the
                # placeholder, so the container is covered at every
                # instant.
                tracked.append(ctx)
                tracked.remove(provisional)
    except BaseException:
        # The preparation or the run failed, or either was interrupted.
        # A terminal Ctrl-C reaches the docker CLI child directly, so
        # the client can die while the daemon still goes on to create
        # the container.  Clean up by name — which also removes the
        # work directory and the credentials written into it — rather
        # than leaving the registry with nothing to act on.
        #
        # The removal happens *before* deregistering, and not after:
        # signals are unblocked again by this point, and an interrupt
        # arriving in the gap would otherwise find an empty registry,
        # exit, and skip the removal below — losing the only
        # actionable handle on the container.  Leaving the entry in
        # place until the removal completes risks nothing worse than a
        # concurrent handler repeating an idempotent ``docker rm -f``.
        _cleanup_container(docker, provisional)
        if tracked is not None and provisional in tracked:
            tracked.remove(provisional)
        raise

    logger.info("  Container ID: %s", cid[:12])
    return ctx


def _wait_for_gerrit_ready(docker: DockerManager, cid: str, timeout: int = 120) -> bool:
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


def _cleanup_container(docker: DockerManager, ctx: _ContainerContext) -> None:
    """Stop and remove a test container and its work directory."""
    with contextlib.suppress(DockerError):
        docker.run_cmd(["stop", "-t", "5", ctx.cid], check=False, timeout=15)
    with contextlib.suppress(DockerError):
        docker.run_cmd(["rm", "-f", ctx.cid], check=False, timeout=10)
    # Clean up work directory
    if ctx.work_dir.exists():
        shutil.rmtree(ctx.work_dir, ignore_errors=True)


def await_gerrit_ready(docker: DockerManager, cid: str) -> str:
    """Block until Gerrit reports ready; return "" or a failure reason.

    On timeout the tail of the container log is emitted at error level
    so the scenario failure is diagnosable without a re-run.
    """
    logger.info("  Waiting for Gerrit to start…")
    ready_timeout = 120
    if _wait_for_gerrit_ready(docker, cid, timeout=ready_timeout):
        logger.info("  Gerrit ready ✅")
        return ""

    error = f"Gerrit did not become ready within {ready_timeout}s"
    logger.error("  %s", error)
    # Dump logs for debugging
    try:
        logs = docker.container_logs(cid, tail=50)
        for line in logs.splitlines()[-20:]:
            logger.error("    %s", line.strip())
    except DockerError as exc:
        logger.debug("Could not dump container logs: %s", exc)
    return error


def wait_initial_cycle(fetch_every: str) -> None:
    """Give pull-replication time for its first fetch cycle."""
    fetch_secs = parse_interval_to_seconds(fetch_every)
    initial_wait = max(fetch_secs + 10, 30)
    logger.info(
        "  Waiting %ds for initial replication cycle (fetchEvery=%s)…",
        initial_wait,
        fetch_every,
    )
    time.sleep(initial_wait)


def release_container(
    docker: DockerManager, ctx: _ContainerContext | None, *, keep: bool
) -> None:
    """Remove the scenario container, or report how to reach it."""
    if ctx is None:
        return
    if not keep:
        logger.info("")
        logger.info("  Cleaning up container %s…", ctx.name)
        _cleanup_container(docker, ctx)
    else:
        logger.info("")
        logger.info(
            "  Container kept running (--keep): %s  "
            "http://localhost:%d  ssh://localhost:%d",
            ctx.name,
            ctx.http_port,
            ctx.ssh_port,
        )
