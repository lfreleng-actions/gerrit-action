# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Docker image commands: existence checks, builds and pulls.

This module owns the image half of the Docker CLI surface — everything
that operates on an image tag rather than on a container instance.  It
builds on :class:`~docker_cli.DockerCommandRunner` for process
execution and error handling.
"""

from __future__ import annotations

import logging

from docker_cli import DockerCommandRunner

logger = logging.getLogger(__name__)


class DockerImageCommands(DockerCommandRunner):
    """``docker image`` / ``build`` / ``pull`` operations."""

    def image_exists(self, image: str) -> bool:
        """Return *True* if *image* exists locally."""
        result = self.run_cmd(
            ["image", "inspect", image],
            check=False,
            timeout=30,
        )
        return result.returncode == 0

    def build_image(
        self,
        tag: str,
        dockerfile_dir: str,
        *,
        dockerfile: str = "Dockerfile",
        build_args: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> None:
        """Build a Docker image.

        Parameters
        ----------
        tag:
            The ``-t`` tag for the built image.
        dockerfile_dir:
            Build context directory (also used as the default location
            for the Dockerfile).
        dockerfile:
            Relative path to the Dockerfile within *dockerfile_dir*.
        build_args:
            Optional ``--build-arg`` key-value pairs.
        timeout:
            Maximum seconds for the build.
        """
        args = [
            "build",
            "-t",
            tag,
            "-f",
            f"{dockerfile_dir}/{dockerfile}",
        ]
        for key, value in (build_args or {}).items():
            args.extend(["--build-arg", f"{key}={value}"])
        args.append(dockerfile_dir)

        logger.info("Building image %s …", tag)
        self.run_cmd(args, timeout=timeout)
        logger.info("Image %s built successfully ✅", tag)

    def pull_image(self, image: str, timeout: int = 300) -> None:
        """Pull an image from a registry."""
        logger.info("Pulling image %s …", image)
        self.run_cmd(["pull", image], timeout=timeout)
