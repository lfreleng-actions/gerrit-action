# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Providing the Docker image that Gerrit instances are started from.

The action prefers a custom image built from the repository's
``Dockerfile`` — Gerrit plus ``uv`` and ``gerrit_to_platform`` — but a
missing Dockerfile or a failed build must never abort the run.  This
module owns that decision and its fallbacks:

- :func:`build_or_reuse_image` — reuse an existing tag, otherwise build
  it, otherwise fall back to the official ``gerritcodereview/gerrit``
  image.
- :func:`verify_custom_image` — report which of the extra components
  actually made it into the image that was built.

Every failure path degrades to the official image rather than raising,
so a broken build environment still yields a usable Gerrit.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import ActionConfig
from docker_manager import DockerManager
from errors import DockerError

logger = logging.getLogger(__name__)


def build_or_reuse_image(
    docker: DockerManager,
    config: ActionConfig,
    dockerfile_dir: Path,
) -> str:
    """Ensure the custom Gerrit image is available and return its tag.

    If the image already exists (e.g. built by a prior Docker layer
    cache step), it is reused.  Otherwise it is built from the
    Dockerfile in *dockerfile_dir*.  If the Dockerfile is missing the
    official ``gerritcodereview/gerrit`` image is used as a fallback.

    Parameters
    ----------
    docker:
        Docker command wrapper used to inspect and build images.
    config:
        Action config supplying the custom tag and the Gerrit version
        used both as a build argument and in the fallback tag.
    dockerfile_dir:
        Directory holding the ``Dockerfile`` and its build context.
    """
    image: str = config.custom_image

    if docker.image_exists(image):
        logger.info("Custom image %s already exists ✅", image)
        return image

    dockerfile_path = dockerfile_dir / "Dockerfile"

    if not dockerfile_path.exists():
        logger.warning(
            "Custom image not found and Dockerfile not available at %s",
            dockerfile_path,
        )
        fallback = f"gerritcodereview/gerrit:{config.gerrit_version}"
        logger.warning("Falling back to official image: %s", fallback)
        return fallback

    logger.info("Building custom Gerrit image with uv and gerrit_to_platform…")
    logger.info("  Base image: gerritcodereview/gerrit:%s", config.gerrit_version)
    logger.info("  Custom image: %s", image)

    try:
        docker.build_image(
            tag=image,
            dockerfile_dir=str(dockerfile_dir),
            build_args={"GERRIT_VERSION": config.gerrit_version},
            timeout=600,
        )
        logger.info("Custom image built successfully ✅")
    except DockerError as exc:
        logger.warning("Failed to build custom image: %s", exc)
        fallback = f"gerritcodereview/gerrit:{config.gerrit_version}"
        logger.warning("Falling back to official image: %s", fallback)
        return fallback

    # Verify components are present in the image
    verify_custom_image(docker, image)

    return image


def verify_custom_image(docker: DockerManager, image: str) -> None:
    """Log verification of uv and gerrit-to-platform inside the image."""
    logger.info("Verifying custom image components…")
    try:
        out = docker.run_ephemeral(
            image, entrypoint="", command=["uv", "--version"], timeout=30
        )
        logger.info("  uv: %s ✅", out.strip())
    except DockerError:
        logger.warning("  uv not found in custom image")

    try:
        out = docker.run_ephemeral(
            image, entrypoint="", command=["which", "change-merged"], timeout=30
        )
        logger.info("  gerrit-to-platform: %s ✅", out.strip())
    except DockerError:
        logger.warning("  gerrit-to-platform not found in custom image")
