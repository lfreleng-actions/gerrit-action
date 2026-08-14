# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Execution of ``docker`` CLI commands via :mod:`subprocess`.

This module owns the single place where the project spawns a Docker
process.  :class:`DockerCommandRunner` turns an argument list into a
``docker <args…>`` invocation and normalises every failure mode
(non-zero exit, timeout, missing binary) into :class:`DockerError`.

Every other ``docker_*`` command module builds on this class, so
timeout handling, debug logging and error translation are defined
exactly once.  Nothing here knows about images or containers — it only
knows how to run a Docker command and report what happened.
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from errors import DockerError

logger = logging.getLogger(__name__)


class DockerCommandRunner:
    """Run ``docker`` commands and translate failures into errors."""

    def run_cmd(
        self,
        args: list[str],
        timeout: int = 60,
        check: bool = True,
        input_data: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run an arbitrary ``docker <args…>`` command.

        Parameters
        ----------
        args:
            Arguments *after* the ``docker`` prefix.  For example,
            ``["ps", "-q"]`` executes ``docker ps -q``.
        timeout:
            Maximum wall-clock seconds before the process is killed.
        check:
            If *True* (the default), raise :class:`DockerError` when the
            process exits with a non-zero return code.
        input_data:
            Optional string piped to the process's standard input.

        Returns
        -------
        subprocess.CompletedProcess[str]
            The completed process with ``stdout`` and ``stderr`` as
            decoded strings.

        Raises
        ------
        DockerError
            If *check* is True and the process exited with a non-zero
            return code.
        DockerError
            If the process did not complete within *timeout* seconds.
        """
        cmd = ["docker", *args]
        logger.debug("Running: %s", shlex.join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_data,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerError(
                f"docker {args[0]} timed out after {timeout}s",
                returncode=-1,
                stderr=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            raise DockerError(
                "docker executable not found – is Docker installed?",
                returncode=-1,
                stderr=str(exc),
            ) from exc

        if check and result.returncode != 0:
            raise DockerError(
                f"docker {args[0]} failed (exit {result.returncode}): "
                f"{result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr,
            )

        logger.debug(
            "docker %s exited %d (stdout=%d bytes, stderr=%d bytes)",
            args[0],
            result.returncode,
            len(result.stdout),
            len(result.stderr),
        )
        return result
