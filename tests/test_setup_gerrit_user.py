# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the per-instance user setup retry policy.

Covers ``_setup_with_retries``, which absorbs the failures a Gerrit
container produces while it is still starting up:

- ``GerritAPIError`` carrying a transient status (401/403/429)
- ``requests`` transport failures — a refused connection, or a 5xx the
  session's own ``urllib3`` retry policy already exhausted and raised
  as ``RetryError``
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from setup_user_instances import _setup_with_retries

from gerrit_api import GerritAPIError


def _raiser(*errors: Exception) -> Any:
    """Return a setup callable raising *errors* in turn, then succeeding."""
    queue = list(errors)

    def _setup(**_kwargs: Any) -> dict[str, Any]:
        """Stand in for ``run_local``."""
        if queue:
            raise queue.pop(0)
        return {"_account_id": 1000000}

    return _setup


@pytest.fixture()
def no_sleep() -> Iterator[MagicMock]:
    """Skip the backoff delays so the retry tests run instantly."""
    with patch("setup_user_instances.time.sleep") as mock_sleep:
        yield mock_sleep


class TestTransportFailures:
    """A container that is still starting up must be retried."""

    def test_exhausted_transport_retry_is_retried(self, no_sleep: MagicMock) -> None:
        """A 503 arrives as RetryError and must reach the backoff.

        The session retries 500/502/503/504 at the transport layer, so
        a container still initialising never surfaces the raw status
        here — it surfaces as a ``RetryError`` once urllib3 gives up.
        """
        setup = _raiser(requests.exceptions.RetryError("too many 503s"))

        assert (
            _setup_with_retries(
                "http://localhost:8080",
                "slug",
                "user",
                ["ssh-ed25519 AAAA... user@example.com"],
                setup_fn=setup,
            )
            is True
        )
        # 3s for the first retry, per _INITIAL_RETRY_DELAY.
        no_sleep.assert_called_once_with(3)

    def test_connection_refused_is_retried(self, no_sleep: MagicMock) -> None:
        """An early-startup connection refusal is not a hard failure."""
        setup = _raiser(
            requests.exceptions.ConnectionError("connection refused"),
            requests.exceptions.ConnectionError("connection refused"),
        )

        assert (
            _setup_with_retries(
                "http://localhost:8080",
                "slug",
                "user",
                ["ssh-ed25519 AAAA... user@example.com"],
                setup_fn=setup,
            )
            is True
        )
        # Delay doubles between attempts: 3s then 6s.
        assert [c.args[0] for c in no_sleep.call_args_list] == [3, 6]

    def test_persistent_transport_failure_gives_up(self, no_sleep: MagicMock) -> None:
        """The attempt budget is still bounded."""
        setup = _raiser(
            *[requests.exceptions.ConnectionError("refused") for _ in range(3)]
        )

        assert (
            _setup_with_retries(
                "http://localhost:8080",
                "slug",
                "user",
                ["ssh-ed25519 AAAA... user@example.com"],
                setup_fn=setup,
            )
            is False
        )
        # Two sleeps for three attempts; the last failure returns.
        assert no_sleep.call_count == 2

    def test_each_retry_is_logged(
        self, no_sleep: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Transport retries are as visible as the API-error ones."""
        setup = _raiser(requests.exceptions.ConnectionError("refused"))

        with caplog.at_level("WARNING", logger="setup_user_instances"):
            _setup_with_retries(
                "http://localhost:8080",
                "slug",
                "user",
                ["ssh-ed25519 AAAA... user@example.com"],
                setup_fn=setup,
            )

        assert any("Attempt 1/3 failed" in r.message for r in caplog.records)


class TestApiErrors:
    """The pre-existing status-keyed behaviour is unchanged."""

    def test_transient_status_is_retried(self, no_sleep: MagicMock) -> None:
        """403 during auth-subsystem startup is retried."""
        setup = _raiser(GerritAPIError("forbidden", status_code=403))

        assert (
            _setup_with_retries(
                "http://localhost:8080",
                "slug",
                "user",
                ["ssh-ed25519 AAAA... user@example.com"],
                setup_fn=setup,
            )
            is True
        )
        no_sleep.assert_called_once_with(3)

    def test_non_transient_status_fails_immediately(self, no_sleep: MagicMock) -> None:
        """A 400 is a real rejection and must not be retried."""
        setup = _raiser(GerritAPIError("bad request", status_code=400))

        assert (
            _setup_with_retries(
                "http://localhost:8080",
                "slug",
                "user",
                ["ssh-ed25519 AAAA... user@example.com"],
                setup_fn=setup,
            )
            is False
        )
        no_sleep.assert_not_called()

    def test_unexpected_error_is_not_retried(self, no_sleep: MagicMock) -> None:
        """Anything outside the two transient families still aborts."""
        setup = _raiser(ValueError("programming error"))

        assert (
            _setup_with_retries(
                "http://localhost:8080",
                "slug",
                "user",
                ["ssh-ed25519 AAAA... user@example.com"],
                setup_fn=setup,
            )
            is False
        )
        no_sleep.assert_not_called()
