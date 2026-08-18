# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Launching the Gerrit container and recording what came back.

Once a site directory has been initialised, configured and populated
with plugins, this module performs the final step: turning it into a
running container and capturing the facts later steps need — the
container ID and IP, and the metadata block persisted to
``instances.json``.

Failure to start is reported as ``None`` rather than an exception, so
the orchestrator can mark a single instance as failed and carry on
with the rest.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import ActionConfig, ApiPathStore, InstanceConfig
from docker_manager import DockerManager
from errors import DockerError
from startup_endpoints import InstanceEndpoints
from startup_site_layout import gerrit_volume_mounts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunningContainer:
    """Identity of a container that has just been started.

    Attributes
    ----------
    name:
        The ``--name`` given to the container (``gerrit-<slug>``).
    cid:
        Full container ID returned by ``docker run``.
    ip:
        First IP address of the container, or ``""`` if it could not
        be determined.
    """

    name: str
    cid: str
    ip: str


def remove_bundled_replication_plugin(instance_dir: Path) -> None:
    """Delete the image's bundled ``replication.jar`` if present.

    The bundled push-replication plugin conflicts with the
    pull-replication plugin this action installs, so it is removed
    before the container starts.
    """
    bundled = instance_dir / "plugins" / "replication.jar"
    if bundled.exists():
        bundled.unlink()


def launch_gerrit_container(
    docker: DockerManager,
    config: ActionConfig,
    instance_dir: Path,
    slug: str,
    endpoints: InstanceEndpoints,
    image: str,
) -> RunningContainer | None:
    """Start the Gerrit container for *slug*.

    Returns the :class:`RunningContainer` on success, or *None* if
    ``docker run`` failed (the error is logged before returning).
    """
    logger.info("Starting Gerrit container…")

    container_name = f"gerrit-{slug}"
    cidfile = str(config.work_path / f"{container_name}.cid")

    # Volume mounts
    volumes: dict[str, str] = gerrit_volume_mounts(instance_dir)

    # Add SSH volume (read-only) when using SSH auth
    if config.auth_type.lower() == "ssh":
        volumes[f"{instance_dir / 'ssh'}:ro"] = "/var/gerrit/ssh"

    # Environment variables
    env: dict[str, str] = {
        "CANONICAL_WEB_URL": endpoints.canonical_url,
        "HTTPD_LISTEN_URL": endpoints.listen_url,
    }
    if config.debug:
        env["DEBUG"] = "1"

    try:
        cid = docker.run_container(
            image=image,
            name=container_name,
            ports={
                endpoints.local_http_port: 8080,
                endpoints.local_ssh_port: 29418,
            },
            volumes=volumes,
            env=env,
            cidfile=cidfile,
            detach=True,
            timeout=60,
        )
    except DockerError as exc:
        logger.error("Failed to start Gerrit container for %s: %s ❌", slug, exc)
        return None

    # Wait for container process to settle
    time.sleep(2)

    # Get container IP
    try:
        container_ip = docker.container_ip(cid)
    except DockerError:
        container_ip = ""

    # Record container ID
    cid_file = config.work_path / "container_ids.txt"
    with open(cid_file, "a", encoding="utf-8") as fh:
        fh.write(f"{cid}\n")

    return RunningContainer(name=container_name, cid=cid, ip=container_ip)


def build_instance_metadata(
    instance: InstanceConfig,
    endpoints: InstanceEndpoints,
    container: RunningContainer,
    api_path_store: ApiPathStore,
    expected_count: int,
    ssh_host_keys: dict[str, str],
) -> dict[str, Any]:
    """Assemble the ``instances.json`` record for a started instance."""
    return {
        "cid": container.cid,
        "ip": container.ip,
        "http_port": endpoints.local_http_port,
        "ssh_port": endpoints.local_ssh_port,
        "url": f"http://{container.ip}:8080" if container.ip else "",
        "gerrit_host": instance.gerrit_host,
        "project": instance.project,
        "api_path": api_path_store.get_api_path(instance.slug),
        "api_url": api_path_store.get_api_url(instance.slug),
        "expected_project_count": expected_count,
        "ssh_host_keys": ssh_host_keys,
    }


def report_started_instance(
    docker: DockerManager,
    config: ActionConfig,
    container: RunningContainer,
    endpoints: InstanceEndpoints,
    api_url: str,
) -> None:
    """Log the success summary, plus ``docker ps`` output when debugging."""
    logger.info("✅ Gerrit instance started")
    logger.info(
        "   Container ID: %s", container.cid[:12] if container.cid else "(unknown)"
    )
    logger.info("   IP Address: %s", container.ip or "(unknown)")
    if container.ip:
        logger.info("   HTTP URL: http://%s:8080", container.ip)
    logger.info("   SSH URL: ssh://localhost:%d", endpoints.local_ssh_port)
    logger.info("   Source API URL: %s", api_url)
    logger.info("")

    if config.debug:
        try:
            ps_output = docker.ps(filter_name=container.name)
            logger.debug("Container status:\n%s", ps_output)
        except DockerError as exc:
            logger.debug("Could not query container status: %s", exc)
