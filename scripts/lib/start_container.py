# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Start-up planning, container launch and run bookkeeping.

Split out of ``start-instances.py``.  Resolves the ports and URLs an
instance will be reachable at, runs the container once its site
directory is provisioned, records the resulting metadata in
``instances.json``, and formats the run-level summaries.  The step
sequence that drives all of this stays in ``start-instances.py``, which
re-exports the pieces here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import ActionConfig, ApiPathStore, InstanceConfig, InstanceStore
from docker_manager import DockerManager
from errors import DockerError
from start_model import InstancePlan, InstanceStartOptions
from start_site import site_volumes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartedContainer:
    """Identity of a container that ``docker run`` has accepted."""

    cid: str
    ip: str
    name: str


# ---------------------------------------------------------------------------
# Start-up planning
# ---------------------------------------------------------------------------


def _resolve_tunnel(
    slug: str,
    config: ActionConfig,
) -> tuple[bool, str, int, int]:
    """Determine tunnel configuration for an instance.

    Returns ``(use_tunnel, url_host, url_http_port, url_ssh_port)``.
    The ports returned are the *external* ports to advertise (either
    tunnel ports or the local mapped ports — the caller decides local
    ports separately).
    """
    tunnel_ports = config.tunnel_ports
    tunnel_host = config.tunnel_host

    if tunnel_host and slug in tunnel_ports:
        tc = tunnel_ports[slug]
        logger.info("  External tunnel configured: %s", tunnel_host)
        logger.info("    HTTP port: %d", tc.http_port)
        logger.info("    SSH port: %d", tc.ssh_port)
        return True, tunnel_host, tc.http_port, tc.ssh_port

    if tunnel_host:
        logger.info("  TUNNEL_HOST set but no ports found for slug '%s'", slug)
        logger.info("  Falling back to localhost URLs")

    return False, "localhost", 0, 0  # 0 → caller fills in local ports


def _build_urls(
    url_host: str,
    url_http_port: int,
    effective_api_path: str,
    api_path: str,
) -> tuple[str, str]:
    """Return ``(canonical_url, listen_url)`` for an instance."""
    if effective_api_path:
        logger.info("  Using API path: %s (USE_API_PATH=true)", effective_api_path)
        return (
            f"http://{url_host}:{url_http_port}{effective_api_path}/",
            f"http://*:8080{effective_api_path}/",
        )

    if api_path:
        logger.info("  API path detected (%s) but USE_API_PATH is false", api_path)
        logger.info("  Serving at root instead")
    return f"http://{url_host}:{url_http_port}/", "http://*:8080/"


def plan_instance_startup(
    instance: InstanceConfig,
    options: InstanceStartOptions,
) -> InstancePlan:
    """Resolve the ports, URLs and SSH identity for one instance."""
    config = options.config
    slug = instance.slug

    # Local ports
    http_port = config.base_http_port + options.index
    ssh_port = config.base_ssh_port + options.index

    # API path from detection phase
    api_path = options.api_path_store.get_api_path(slug)

    # Tunnel configuration
    use_tunnel, url_host, tunnel_http, tunnel_ssh = _resolve_tunnel(slug, config)
    if use_tunnel:
        url_http_port = tunnel_http
        url_ssh_port = tunnel_ssh
    else:
        url_host = "localhost"
        url_http_port = http_port
        url_ssh_port = ssh_port

    canonical_url, listen_url = _build_urls(
        url_host, url_http_port, instance.effective_api_path, api_path
    )

    return InstancePlan(
        slug=slug,
        gerrit_host=instance.gerrit_host,
        project=instance.project,
        # Per-instance SSH settings
        remote_ssh_user=instance.ssh_user or config.remote_ssh_user,
        remote_ssh_port=instance.ssh_port or config.remote_ssh_port,
        http_port=http_port,
        ssh_port=ssh_port,
        api_path=api_path,
        api_url=options.api_path_store.get_api_url(slug),
        use_tunnel=use_tunnel,
        url_host=url_host,
        url_http_port=url_http_port,
        url_ssh_port=url_ssh_port,
        advertised_ssh_addr=f"{url_host}:{url_ssh_port}",
        canonical_url=canonical_url,
        listen_url=listen_url,
    )


def log_instance_banner(plan: InstancePlan, index: int) -> None:
    """Log the banner that introduces an instance's provisioning."""
    logger.info("")
    logger.info("========================================")
    logger.info("Instance %d: %s", index + 1, plan.slug)
    logger.info("  Project: %s", plan.project or "(all)")
    logger.info("  Source: %s", plan.gerrit_host)
    logger.info("  Local HTTP Port: %d", plan.http_port)
    logger.info("  Local SSH Port: %d", plan.ssh_port)
    if plan.use_tunnel:
        logger.info("  Tunnel Mode: ENABLED")
        logger.info("  Public URL: %s", plan.canonical_url)
        logger.info("  Public SSH: %s", plan.advertised_ssh_addr)
    else:
        logger.info("  Tunnel Mode: disabled (localhost)")
    logger.info("========================================")


# ---------------------------------------------------------------------------
# Container launch
# ---------------------------------------------------------------------------


def launch_container(
    docker: DockerManager,
    plan: InstancePlan,
    options: InstanceStartOptions,
    instance_dir: Path,
) -> StartedContainer | None:
    """Run the Gerrit container for *plan*.

    Returns the started container's identity, or *None* when Docker
    refused to start it.
    """
    logger.info("Starting Gerrit container…")

    config = options.config
    container_name = f"gerrit-{plan.slug}"
    cidfile = str(config.work_path / f"{container_name}.cid")

    # Volume mounts
    volumes = site_volumes(instance_dir)

    # Add SSH volume (read-only) when using SSH auth
    if config.auth_type.lower() == "ssh":
        volumes[f"{instance_dir / 'ssh'}:ro"] = "/var/gerrit/ssh"

    # Environment variables
    env: dict[str, str] = {
        "CANONICAL_WEB_URL": plan.canonical_url,
        "HTTPD_LISTEN_URL": plan.listen_url,
    }
    if config.debug:
        env["DEBUG"] = "1"

    try:
        cid = docker.run_container(
            image=options.image,
            name=container_name,
            ports={plan.http_port: 8080, plan.ssh_port: 29418},
            volumes=volumes,
            env=env,
            cidfile=cidfile,
            detach=True,
            timeout=60,
        )
    except DockerError as exc:
        logger.error("Failed to start Gerrit container for %s: %s ❌", plan.slug, exc)
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

    return StartedContainer(cid=cid, ip=container_ip, name=container_name)


def finish_instance(
    docker: DockerManager,
    plan: InstancePlan,
    options: InstanceStartOptions,
    started: StartedContainer,
    expected_count: int,
    ssh_host_keys: dict[str, str],
) -> None:
    """Persist instance metadata and report the started container."""
    cid = started.cid
    container_ip = started.ip

    metadata: dict[str, Any] = {
        "cid": cid,
        "ip": container_ip,
        "http_port": plan.http_port,
        "ssh_port": plan.ssh_port,
        "url": f"http://{container_ip}:8080" if container_ip else "",
        "gerrit_host": plan.gerrit_host,
        "project": plan.project,
        "api_path": plan.api_path,
        "api_url": plan.api_url,
        "expected_project_count": expected_count,
        "ssh_host_keys": ssh_host_keys,
    }
    options.instance_store.set_instance(plan.slug, metadata)
    options.instance_store.save()

    logger.info("✅ Gerrit instance started")
    logger.info("   Container ID: %s", cid[:12] if cid else "(unknown)")
    logger.info("   IP Address: %s", container_ip or "(unknown)")
    if container_ip:
        logger.info("   HTTP URL: http://%s:8080", container_ip)
    logger.info("   SSH URL: ssh://localhost:%d", plan.ssh_port)
    logger.info("   Source API URL: %s", plan.api_url)
    logger.info("")

    if options.config.debug:
        try:
            ps_output = docker.ps(filter_name=started.name)
            logger.debug("Container status:\n%s", ps_output)
        except DockerError as exc:
            logger.debug("Could not query container status: %s", exc)


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------


def prepare_run_state(config: ActionConfig) -> tuple[InstanceStore, ApiPathStore]:
    """Reset the work directory's tracking files and load the API paths."""
    work_dir = config.work_path
    work_dir.mkdir(parents=True, exist_ok=True)

    # Initialise tracking files
    cid_file = work_dir / "container_ids.txt"
    cid_file.write_text("", encoding="utf-8")

    instance_store = InstanceStore(config.instances_json_path)
    # Start with empty data (don't try to load — file may not exist yet)
    instance_store._data = {}
    instance_store.save()

    # Load API paths from detection phase
    api_path_store = ApiPathStore(config.api_paths_json_path)
    api_path_store.load()

    return instance_store, api_path_store


def log_startup_totals(failed: int, total: int) -> None:
    """Log the closing banner for a whole run."""
    logger.info("========================================")
    if failed == 0:
        logger.info("All instances started! ✅")
    else:
        logger.error("%d of %d instances failed to start ❌", failed, total)
    logger.info("Total instances: %d", total)
    logger.info("========================================")
    logger.info("")


def format_startup_summary(instance_store: InstanceStore) -> str:
    """Render the step summary table for started instances."""
    lines = [
        "**Instances Started** 🚀",
        "",
        "| Slug | HTTP Port | SSH Port | Status |",
        "|------|-----------|----------|--------|",
    ]
    for slug, meta in instance_store:
        http_port = meta.get("http_port", "?")
        ssh_port = meta.get("ssh_port", "?")
        lines.append(f"| {slug} | {http_port} | {ssh_port} | ✅ Running |")
    lines.append("")
    return "\n".join(lines)
