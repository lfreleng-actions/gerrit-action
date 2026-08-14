# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Crossing the container boundary: ``docker exec`` and ``docker cp``.

This module owns the two ways the action reaches inside a running
container — executing a command in it, and copying files in or out.
Both are grouped here because they share the same trust boundary: the
arguments end up being interpreted inside the container.
"""

from __future__ import annotations

import logging

from docker_cli import DockerCommandRunner

logger = logging.getLogger(__name__)


class DockerExecCommands(DockerCommandRunner):
    """``docker exec`` / ``docker cp`` operations."""

    def exec_cmd(
        self,
        cid: str,
        command: str,
        timeout: int = 30,
        check: bool = True,
        user: str | None = None,
    ) -> str:
        """Execute a command inside a running container.

        Parameters
        ----------
        cid:
            Container ID or name.
        command:
            Shell command string (run via ``sh -c``).
        timeout:
            Maximum seconds to wait.
        check:
            If *True*, raise :class:`DockerError` on non-zero exit.
        user:
            Optional user to run the command as (passed to
            ``docker exec -u``).  For example, ``"0"`` or
            ``"root"`` to run as root.

        Returns
        -------
        str
            Trimmed stdout of the command.
        """
        cmd: list[str] = ["exec"]
        if user is not None:
            cmd.extend(["-u", user])
        cmd.extend([cid, "sh", "-c", command])
        result = self.run_cmd(
            cmd,
            timeout=timeout,
            check=check,
        )
        return result.stdout.strip()

    def exec_test(self, cid: str, test_args: str) -> bool:
        """Run ``test <test_args>`` inside a container.

        Returns *True* if the test succeeds (exit code 0), *False*
        otherwise.  Never raises :class:`DockerError`.
        """
        result = self.run_cmd(
            ["exec", cid, "test", *test_args.split()],
            check=False,
            timeout=15,
        )
        return result.returncode == 0

    def cp(self, src: str, dst: str, timeout: int = 30) -> None:
        """Copy files between a container and the local filesystem.

        Parameters
        ----------
        src:
            Source path (``container:path`` or local path).
        dst:
            Destination path (``container:path`` or local path).
        """
        self.run_cmd(["cp", src, dst], timeout=timeout)
