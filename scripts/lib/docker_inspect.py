# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read-only observation of containers: inspect, state and logs.

This module owns the commands that report on a container without
changing it — ``docker inspect`` (and the narrow accessors built on
it) plus ``docker logs``.  Keeping them apart from
:mod:`docker_containers` makes it obvious which calls are safe to make
against a live container during health checks and diagnostics.
"""

from __future__ import annotations

import logging

from docker_cli import DockerCommandRunner

logger = logging.getLogger(__name__)


class DockerInspectCommands(DockerCommandRunner):
    """``docker inspect`` / ``docker logs`` operations."""

    def inspect(self, cid: str, format_str: str = "") -> str:
        """Run ``docker inspect`` and return the (formatted) output.

        Parameters
        ----------
        cid:
            Container ID or name.
        format_str:
            Go-template format string (e.g. ``"{{.State.Status}}"``).

        Returns
        -------
        str
            Trimmed stdout of the inspect command.

        Raises
        ------
        DockerError
            If the container does not exist.
        """
        args = ["inspect"]
        if format_str:
            args.extend(["-f", format_str])
        args.append(cid)
        result = self.run_cmd(args, timeout=15)
        return result.stdout.strip()

    def container_state(self, cid: str) -> str:
        """Return the container state (e.g. ``"running"``, ``"exited"``)."""
        return self.inspect(cid, "{{.State.Status}}")

    def container_ip(self, cid: str) -> str:
        """Return the first IP address of a container."""
        return self.inspect(
            cid, "{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
        )

    def container_exists(self, cid: str) -> bool:
        """Return *True* if the container exists (regardless of state)."""
        result = self.run_cmd(["inspect", cid], check=False, timeout=15)
        return result.returncode == 0

    def container_logs(self, cid: str, tail: int = 500) -> str:
        """Return the last *tail* lines of container logs.

        Both stdout and stderr streams are captured and merged.
        """
        result = self.run_cmd(
            ["logs", "--tail", str(tail), cid],
            timeout=30,
        )
        # Docker writes most log output to stderr for containers
        return result.stdout + result.stderr

    def grep_logs(
        self,
        cid: str,
        pattern: str,
        tail: int = 1000,
    ) -> bool:
        """Check whether *pattern* appears in the container's recent logs.

        This replaces the duplicated ``check_plugin_in_logs()`` function
        that existed in two shell scripts.

        Parameters
        ----------
        cid:
            Container ID or name.
        pattern:
            Plain substring to search for (not a regex).
        tail:
            Number of log lines to inspect.

        Returns
        -------
        bool
            *True* if *pattern* was found.
        """
        logs = self.container_logs(cid, tail=tail)
        found = pattern in logs
        if found:
            logger.debug("Pattern %r found in logs of %s", pattern, cid[:12])
        else:
            logger.debug("Pattern %r NOT found in logs of %s", pattern, cid[:12])
        return found
