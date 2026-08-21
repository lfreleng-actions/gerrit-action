# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Plugin JAR downloads for a Gerrit instance.

Split out of ``start-instances.py``.  Fetches the pull-replication
plugin (with a local cache and a fallback source) plus any extra
plugin URLs the caller supplies.  Both entry points take the download
helper and the cache directory as arguments so ``start-instances.py``
stays the single place those seams are resolved; they are re-exported
from there as ``download_plugin`` and ``download_additional_plugins``.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

import requests
from start_model import _PLUGIN_ALT_URL_TEMPLATE, _PLUGIN_URL_TEMPLATE

logger = logging.getLogger(__name__)

# A download step: fetch a URL to a destination, reporting success.
Downloader = Callable[[str, Path], bool]


def install_pull_replication_plugin(
    plugin_dir: Path,
    plugin_version: str,
    cache_dir: Path,
    download: Downloader,
) -> bool:
    """Place the pull-replication plugin JAR in *plugin_dir*.

    Uses *cache_dir* as a local JAR cache and tries a fallback URL if
    the primary CI build is unavailable.

    Returns *True* on success, *False* on failure.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_jar = cache_dir / f"pull-replication-{plugin_version}.jar"
    target_jar = plugin_dir / "pull-replication.jar"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    if cached_jar.exists():
        logger.info("Using cached plugin: %s", cached_jar)
        shutil.copy2(cached_jar, target_jar)
        return True

    logger.info("Downloading pull-replication plugin…")

    # Primary URL
    primary_url = _PLUGIN_URL_TEMPLATE.format(version=plugin_version)
    if download(primary_url, cached_jar):
        logger.info("Plugin downloaded ✅")
        shutil.copy2(cached_jar, target_jar)
        return True

    # Fallback URL
    logger.warning("Primary download failed, attempting alternate source…")
    alt_url = _PLUGIN_ALT_URL_TEMPLATE.format(version=plugin_version)
    if download(alt_url, cached_jar):
        logger.info("Plugin downloaded from alternate source ✅")
        shutil.copy2(cached_jar, target_jar)
        return True

    logger.error("Failed to download pull-replication plugin ❌")
    return False


def install_extra_plugins(
    plugin_dir: Path,
    additional_plugins: str,
    download: Downloader,
) -> None:
    """Download the comma-separated *additional_plugins* URLs."""
    logger.info("Downloading additional plugins…")
    plugin_dir.mkdir(parents=True, exist_ok=True)

    for url in additional_plugins.split(","):
        url = url.strip()
        if not url:
            continue
        name = url.rsplit("/", 1)[-1]
        dest = plugin_dir / name
        logger.info("Downloading: %s", name)
        if download(url, dest):
            logger.info("  ✅ %s", name)
        else:
            logger.warning("  Failed to download %s", name)


def _download_file(url: str, dest: Path) -> bool:
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
