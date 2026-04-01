# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Domain-specific exception hierarchy for gerrit-action.

All exceptions raised by gerrit-action library modules inherit from
:class:`GerritActionError`, making it easy to catch every anticipated
failure at the CLI entry-point level while still allowing callers to
handle specific categories (Docker, health-check, replication, …)
individually.
"""

from __future__ import annotations


class GerritActionError(Exception):
    """Base exception for all gerrit-action errors."""


class DockerError(GerritActionError):
    """A Docker CLI command failed.

    Attributes:
        returncode: Exit code returned by the Docker process.
        stderr: Standard error output captured from the process.
    """

    def __init__(self, message: str, returncode: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr

    def __str__(self) -> str:
        base = super().__str__()
        if self.stderr:
            return f"{base}\nstderr: {self.stderr.strip()}"
        return base


class HealthCheckError(GerritActionError):
    """A health check did not pass within the allowed retries.

    Attributes:
        url: The URL (or description) that was being checked.
        last_status_code: The most recent HTTP status code, or ``None``
            if the check never received an HTTP response.
        attempts: Total number of attempts made before giving up.
    """

    def __init__(
        self,
        message: str,
        url: str = "",
        last_status_code: int | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.last_status_code = last_status_code
        self.attempts = attempts


class ReplicationError(GerritActionError):
    """Replication did not complete within the timeout.

    Attributes:
        expected_count: Number of repositories expected.
        actual_count: Number of repositories replicated so far.
        elapsed: Wall-clock seconds elapsed before the timeout.
    """

    def __init__(
        self,
        message: str,
        expected_count: int = 0,
        actual_count: int = 0,
        elapsed: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.expected_count = expected_count
        self.actual_count = actual_count
        self.elapsed = elapsed


class ConfigError(GerritActionError):
    """Invalid or missing configuration."""


class ApiPathError(GerritActionError):
    """API path detection or validation failed."""


class PluginError(GerritActionError):
    """A required Gerrit plugin is missing or failed to load."""


# ---------------------------------------------------------------------------
# G2P (gerrit_to_platform) errors
# ---------------------------------------------------------------------------


class G2PError(GerritActionError):
    """Base exception for G2P operations."""


class G2PConfigError(G2PError):
    """G2P configuration is invalid or incomplete."""


class G2PCheckError(G2PError):
    """A GitHub-side check failed in strict (error) mode.

    Attributes:
        failed_checks: Names of the checks that did not pass.
    """

    def __init__(
        self,
        message: str,
        failed_checks: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.failed_checks: list[str] = failed_checks or []


class G2PSetupError(G2PError):
    """Failed to set up G2P inside the Gerrit container."""
