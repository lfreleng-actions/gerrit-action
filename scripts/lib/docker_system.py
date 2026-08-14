# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Daemon-wide Docker queries and housekeeping.

This module owns the commands that are not scoped to a single
container or image: listing what is running (``docker ps``) and
reclaiming space (``docker system prune``).
"""

from __future__ import annotations

import logging

from docker_cli import DockerCommandRunner

logger = logging.getLogger(__name__)


class DockerSystemCommands(DockerCommandRunner):
    """``docker ps`` / ``docker system prune`` operations."""

    def ps(
        self,
        *,
        filter_name: str = "",
        quiet: bool = False,
    ) -> str:
        """Run ``docker ps`` with optional filters.

        Parameters
        ----------
        filter_name:
            If non-empty, filter by container name prefix.
        quiet:
            If *True*, return only container IDs (``-q``).

        Returns
        -------
        str
            The raw stdout of ``docker ps``.
        """
        args: list[str] = ["ps"]
        if quiet:
            args.append("-q")
        if filter_name:
            args.extend(["-f", f"name={filter_name}"])
        result = self.run_cmd(args, check=False, timeout=15)
        return result.stdout.strip()

    def system_prune(
        self,
        *,
        force: bool = True,
        filters: list[str] | None = None,
    ) -> None:
        """Run ``docker system prune``.

        Parameters
        ----------
        force:
            Pass ``-f`` to skip the confirmation prompt.
        filters:
            List of ``--filter`` arguments (e.g. ``["until=24h"]``).
        """
        args = ["system", "prune"]
        if force:
            args.append("-f")
        for f in filters or []:
            args.extend(["--filter", f])
        self.run_cmd(args, check=False, timeout=60)
