# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""On-disk stores for the JSON state files gerrit-action passes between steps.

Owns reading and writing the two metadata files that let one action step
hand results to the next:

* :class:`InstanceStore` — ``instances.json``, written by
  ``start-instances`` and consumed by every later step.
* :class:`ApiPathStore` — ``api_paths.json``, written by
  ``detect-api-paths``.

Between them they replace the ``jq`` iteration boilerplate that was
previously duplicated across the shell scripts.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from errors import ConfigError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Instance store — reading / writing instances.json
# ---------------------------------------------------------------------------


class InstanceStore:
    """Read and write the ``instances.json`` metadata file.

    This replaces the ``jq`` iteration boilerplate duplicated in 6+
    shell scripts.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def load(self) -> dict[str, dict[str, Any]]:
        """Load instances from disk.

        Raises :class:`ConfigError` if the file does not exist.
        """
        if not self.path.exists():
            raise ConfigError(f"Instances file not found: {self.path}")
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in {self.path}: {exc}") from exc
        return self._data

    def save(self) -> None:
        """Persist the current data back to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def data(self) -> dict[str, dict[str, Any]]:
        """Return the raw instance data dict."""
        return self._data

    def slugs(self) -> list[str]:
        """Return sorted list of instance slugs."""
        return sorted(self._data.keys())

    def get(self, slug: str) -> dict[str, Any]:
        """Return metadata for *slug*, raising :class:`ConfigError` if missing."""
        if slug not in self._data:
            raise ConfigError(f"Instance '{slug}' not found in {self.path}")
        return self._data[slug]

    def __iter__(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Iterate over ``(slug, metadata)`` pairs, sorted by slug."""
        for slug in self.slugs():
            yield slug, self._data[slug]

    def __len__(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def set_instance(self, slug: str, metadata: dict[str, Any]) -> None:
        """Add or update the metadata for *slug*."""
        self._data[slug] = metadata

    def update_field(self, slug: str, key: str, value: Any) -> None:
        """Update a single field for *slug*."""
        if slug not in self._data:
            self._data[slug] = {}
        self._data[slug][key] = value


# ---------------------------------------------------------------------------
# API paths store
# ---------------------------------------------------------------------------


class ApiPathStore:
    """Read and write the ``api_paths.json`` file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, str]] = {}

    def load(self) -> dict[str, dict[str, str]]:
        """Load API paths from disk; returns empty dict if file missing."""
        if not self.path.exists():
            self._data = {}
            return self._data
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON in %s: %s", self.path, exc)
            self._data = {}
        return self._data

    def save(self) -> None:
        """Persist the current data back to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2) + "\n",
            encoding="utf-8",
        )

    @property
    def data(self) -> dict[str, dict[str, str]]:
        return self._data

    def set_path(
        self,
        slug: str,
        *,
        gerrit_host: str,
        api_path: str,
        api_url: str,
    ) -> None:
        """Record the detected API path for *slug*."""
        self._data[slug] = {
            "gerrit_host": gerrit_host,
            "api_path": api_path,
            "api_url": api_url,
        }

    def get_api_path(self, slug: str) -> str:
        """Return the API path for *slug*, defaulting to ``""``."""
        entry = self._data.get(slug, {})
        return entry.get("api_path", "")

    def get_api_url(self, slug: str) -> str:
        """Return the full API URL for *slug*, defaulting to ``""``."""
        entry = self._data.get(slug, {})
        return entry.get("api_url", "")
