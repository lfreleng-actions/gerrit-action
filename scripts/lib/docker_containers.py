# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Container lifecycle commands: run, stop, kill and remove.

This module owns the commands that create and destroy containers.  It
covers both long-lived containers (:meth:`DockerContainerCommands.
run_container`, which returns a container ID for later inspection) and
one-shot ``--rm`` containers (:meth:`DockerContainerCommands.
run_ephemeral`, used for operations such as ``gerrit init`` that need
to execute inside the Gerrit image and then be discarded).

Read-only observation of a running container lives in
:mod:`docker_inspect`; executing commands inside one lives in
:mod:`docker_exec`.
"""

from __future__ import annotations

import logging

from docker_cli import DockerCommandRunner

logger = logging.getLogger(__name__)


class DockerContainerCommands(DockerCommandRunner):
    """``docker run`` / ``stop`` / ``kill`` / ``rm`` operations."""

    def run_container(
        self,
        image: str,
        name: str,
        *,
        ports: dict[int, int] | None = None,
        volumes: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        cidfile: str | None = None,
        detach: bool = True,
        remove: bool = False,
        extra_args: list[str] | None = None,
        command: str | list[str] | None = None,
        timeout: int = 60,
    ) -> str:
        """Start a container and return its ID.

        Parameters
        ----------
        image:
            Docker image to run.
        name:
            Container name (``--name``).
        ports:
            Host→container port mappings (``-p host:container``).
        volumes:
            Host-path→container-path volume mounts (``-v host:container``).
            Append ``:ro`` to the host path for read-only mounts.
        env:
            Environment variables (``-e KEY=VALUE``).
        cidfile:
            If given, write the container ID to this file.
        detach:
            Run in detached mode (``-d``).  Default *True*.
        remove:
            Automatically remove the container when it stops (``--rm``).
        extra_args:
            Additional raw arguments inserted before the image name.
        command:
            Optional command (and arguments) to pass to the container
            entrypoint.
        timeout:
            Maximum seconds to wait for the ``docker run`` command.

        Returns
        -------
        str
            The full container ID (from stdout when detached).
        """
        args: list[str] = ["run"]
        if detach:
            args.append("-d")
        if remove:
            args.append("--rm")
        args.extend(["--name", name])

        if cidfile:
            args.extend(["--cidfile", cidfile])
        for host_port, container_port in (ports or {}).items():
            args.extend(["-p", f"{host_port}:{container_port}"])
        for host_path, container_path in (volumes or {}).items():
            # Support read-only mounts: if host_path ends with :ro, keep it
            if host_path.endswith(":ro"):
                base_path = host_path[:-3]
                args.extend(["-v", f"{base_path}:{container_path}:ro"])
            else:
                args.extend(["-v", f"{host_path}:{container_path}"])
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])

        if extra_args:
            args.extend(extra_args)

        args.append(image)

        if command:
            if isinstance(command, str):
                args.append(command)
            else:
                args.extend(command)

        result = self.run_cmd(args, timeout=timeout)
        cid = result.stdout.strip()
        logger.info(
            "Container %s started: %s",
            name,
            cid[:12] if cid else "(no id)",
        )
        return cid

    def stop(self, cid: str, timeout: int = 30) -> None:
        """Stop a running container.

        Sends SIGTERM, waits up to *timeout* seconds, then SIGKILL.
        Silently succeeds if the container is already stopped.
        """
        logger.info("Stopping container %s …", cid[:12])
        self.run_cmd(
            ["stop", "--time", str(timeout), cid],
            check=False,
            timeout=timeout + 10,
        )

    def kill(self, cid: str) -> None:
        """Send SIGKILL to a container."""
        self.run_cmd(["kill", cid], check=False, timeout=15)

    def remove(self, cid: str, force: bool = False) -> None:
        """Remove a container.

        Parameters
        ----------
        cid:
            Container ID or name.
        force:
            If *True*, pass ``-f`` to remove even if running.
        """
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(cid)
        self.run_cmd(args, check=False, timeout=30)
        logger.info("Container %s removed", cid[:12])

    def run_ephemeral(
        self,
        image: str,
        *,
        volumes: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        command: str | list[str] | None = None,
        entrypoint: str | None = None,
        timeout: int = 120,
    ) -> str:
        """Run a container with ``--rm`` and return its stdout.

        This is intended for one-shot operations like ``gerrit init``
        that need to run inside the Gerrit image and then be discarded.

        Parameters
        ----------
        image:
            Docker image to run.
        volumes:
            Host→container volume mounts.
        env:
            Environment variables.
        command:
            Command to run.
        entrypoint:
            Override the image entrypoint.
        timeout:
            Maximum seconds to wait.

        Returns
        -------
        str
            The stdout output of the container.
        """
        args: list[str] = ["run", "--rm"]

        if entrypoint is not None:
            args.extend(["--entrypoint", entrypoint])
        for host_path, container_path in (volumes or {}).items():
            if ":ro" in host_path:
                base_path = host_path.replace(":ro", "")
                args.extend(["-v", f"{base_path}:{container_path}:ro"])
            else:
                args.extend(["-v", f"{host_path}:{container_path}"])
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])

        args.append(image)

        if command:
            if isinstance(command, str):
                args.append(command)
            else:
                args.extend(command)

        result = self.run_cmd(args, timeout=timeout)
        return result.stdout
