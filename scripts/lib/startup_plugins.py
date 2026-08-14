# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetching the plugin JARs a Gerrit instance is provisioned with.

Two kinds of plugin land in a site's ``plugins/`` directory before the
container starts:

- :func:`download_plugin` — the ``pull-replication`` JAR that the whole
  replication strategy depends on, resolved from the GerritForge CI
  build with a GitHub release fallback and a host-local cache so
  repeated runs do not re-download it.
- :func:`download_additional_plugins` — arbitrary user-supplied URLs
  from the ``additional_plugins`` action input.

Download failures are reported through return values and warnings
rather than exceptions: a missing optional plugin should not abort the
run, and the caller decides whether a missing ``pull-replication`` JAR
is fatal for that instance.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Plugin download URLs (primary and fallback)
PLUGIN_URL_TEMPLATE = (
    "https://gerrit-ci.gerritforge.com/job/"
    "plugin-pull-replication-gh-bazel-{version}/"
    "lastSuccessfulBuild/artifact/"
    "bazel-bin/plugins/pull-replication/pull-replication.jar"
)
PLUGIN_ALT_URL_TEMPLATE = (
    "https://github.com/GerritForge/pull-replication/releases/"
    "download/{version}/pull-replication.jar"
)

# Plugin cache directory
PLUGIN_CACHE_DIR = Path("/tmp/gerrit-plugins")


def download_plugin(
    plugin_dir: Path,
    plugin_version: str,
    skip_plugin_install: bool,
) -> bool:
    """Download the pull-replication plugin JAR.

    Uses a local cache at ``/tmp/gerrit-plugins`` and tries a fallback
    URL if the primary CI build is unavailable.

    Returns *True* on success, *False* on failure.
    """
    if skip_plugin_install:
        logger.info("Skipping plugin download (skip_plugin_install=true)")
        return True

    PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached_jar = PLUGIN_CACHE_DIR / f"pull-replication-{plugin_version}.jar"
    target_jar = plugin_dir / "pull-replication.jar"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    if cached_jar.exists():
        logger.info("Using cached plugin: %s", cached_jar)
        shutil.copy2(cached_jar, target_jar)
        return True

    logger.info("Downloading pull-replication plugin…")

    # Primary URL
    primary_url = PLUGIN_URL_TEMPLATE.format(version=plugin_version)
    if download_file(primary_url, cached_jar):
        logger.info("Plugin downloaded ✅")
        shutil.copy2(cached_jar, target_jar)
        return True

    # Fallback URL
    logger.warning("Primary download failed, attempting alternate source…")
    alt_url = PLUGIN_ALT_URL_TEMPLATE.format(version=plugin_version)
    if download_file(alt_url, cached_jar):
        logger.info("Plugin downloaded from alternate source ✅")
        shutil.copy2(cached_jar, target_jar)
        return True

    logger.error("Failed to download pull-replication plugin ❌")
    return False


def download_additional_plugins(
    plugin_dir: Path,
    additional_plugins: str,
) -> None:
    """Download additional plugins from comma-separated URLs."""
    if not additional_plugins:
        return

    logger.info("Downloading additional plugins…")
    plugin_dir.mkdir(parents=True, exist_ok=True)

    for url in additional_plugins.split(","):
        url = url.strip()
        if not url:
            continue
        name = url.rsplit("/", 1)[-1]
        dest = plugin_dir / name
        logger.info("Downloading: %s", name)
        if download_file(url, dest):
            logger.info("  ✅ %s", name)
        else:
            logger.warning("  Failed to download %s", name)


def download_file(url: str, dest: Path) -> bool:
    """Download *url* to *dest*.  Returns *True* on success."""
    try:
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                fh.write(chunk)
        return True
    except requests.RequestException as exc:
        logger.debug("Download failed for %s: %s", url, exc)
        # Clean up partial download
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False
