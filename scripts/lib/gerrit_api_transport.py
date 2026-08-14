# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""HTTP transport layer for the Gerrit REST client.

Owns the single :class:`requests.Session` used for a Gerrit instance —
including its retry policy — plus URL construction relative to Gerrit's
authenticated ``/a/`` prefix, header assembly (notably the XSRF token)
and the four HTTP verb helpers.

Every higher layer (session bootstrap, accounts, SSH keys,
authentication, the public client) builds on :class:`GerritTransport`
and never talks to :mod:`requests` directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import requests
from gerrit_api_protocol import (
    DEFAULT_TIMEOUT,
    _cookie_names_from_header,
    _parse_response,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class GerritTransport:
    """Low-level HTTP plumbing for a single Gerrit server.

    Args:
        base_url: Base URL of the Gerrit server (e.g., "http://localhost:8080")
        verify_ssl: Whether to verify SSL certificates (default: True)
        timeout: Request timeout in seconds (default: 30)
    """

    def __init__(
        self,
        base_url: str,
        verify_ssl: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self._xsrf_token: str | None = None
        self._account_id: int | None = None

        # Create session with retry logic
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _make_url(self, endpoint: str, authenticated: bool = True) -> str:
        """Construct full URL for an endpoint."""
        endpoint = endpoint.lstrip("/")
        if authenticated and not endpoint.startswith("a/"):
            endpoint = f"a/{endpoint}"
        return urljoin(self.base_url + "/", endpoint)

    def _get_headers(self, content_type: str | None = None) -> dict[str, str]:
        """Get headers including XSRF token if available."""
        headers = {"Accept": "application/json"}
        if self._xsrf_token:
            headers["X-Gerrit-Auth"] = self._xsrf_token
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def get(self, endpoint: str, **kwargs: Any) -> Any:
        """
        Make an authenticated GET request.

        Args:
            endpoint: API endpoint (e.g., "accounts/self")
            **kwargs: Additional arguments to pass to requests

        Returns:
            Parsed JSON response
        """
        url = self._make_url(endpoint)
        headers = self._get_headers()
        headers.update(kwargs.pop("headers", {}))
        logger.debug("GET %s", url)

        response = self.session.get(
            url,
            headers=headers,
            timeout=kwargs.pop("timeout", self.timeout),
            verify=self.verify_ssl,
            **kwargs,
        )

        if not response.ok:
            cookie_hdr = response.request.headers.get("Cookie", "")
            cookie_info = (
                ", ".join(sorted(_cookie_names_from_header(cookie_hdr)))
                if cookie_hdr
                else "(none)"
            )
            logger.debug(
                "GET %s → HTTP %s  (cookie names: %s)",
                url,
                response.status_code,
                cookie_info,
            )

        return _parse_response(response)

    def put(
        self,
        endpoint: str,
        data: dict[str, Any] | str | None = None,
        content_type: str = "application/json",
        **kwargs: Any,
    ) -> Any:
        """
        Make an authenticated PUT request.

        Args:
            endpoint: API endpoint
            data: Request body (dict for JSON, str for plain text)
            content_type: Content-Type header value
            **kwargs: Additional arguments to pass to requests

        Returns:
            Parsed JSON response
        """
        url = self._make_url(endpoint)
        headers = self._get_headers(content_type)
        headers.update(kwargs.pop("headers", {}))
        logger.debug("PUT %s", url)

        body: str | None = json.dumps(data) if isinstance(data, dict) else data

        response = self.session.put(
            url,
            data=body,
            headers=headers,
            timeout=kwargs.pop("timeout", self.timeout),
            verify=self.verify_ssl,
            **kwargs,
        )

        if not response.ok:
            cookie_hdr = response.request.headers.get("Cookie", "")
            cookie_info = (
                ", ".join(sorted(_cookie_names_from_header(cookie_hdr)))
                if cookie_hdr
                else "(none)"
            )
            logger.debug(
                "PUT %s → HTTP %s  (cookie names: %s)",
                url,
                response.status_code,
                cookie_info,
            )

        return _parse_response(response)

    def post(
        self,
        endpoint: str,
        data: dict[str, Any] | str | None = None,
        content_type: str = "application/json",
        **kwargs: Any,
    ) -> Any:
        """
        Make an authenticated POST request.

        Args:
            endpoint: API endpoint
            data: Request body (dict for JSON, str for plain text)
            content_type: Content-Type header value
            **kwargs: Additional arguments to pass to requests

        Returns:
            Parsed JSON response
        """
        url = self._make_url(endpoint)
        headers = self._get_headers(content_type)
        headers.update(kwargs.pop("headers", {}))
        logger.debug("POST %s", url)

        body: str | None = json.dumps(data) if isinstance(data, dict) else data

        response = self.session.post(
            url,
            data=body,
            headers=headers,
            timeout=kwargs.pop("timeout", self.timeout),
            verify=self.verify_ssl,
            **kwargs,
        )

        if not response.ok:
            cookie_hdr = response.request.headers.get("Cookie", "")
            cookie_info = (
                ", ".join(sorted(_cookie_names_from_header(cookie_hdr)))
                if cookie_hdr
                else "(none)"
            )
            logger.debug(
                "POST %s → HTTP %s  (cookie names: %s)",
                url,
                response.status_code,
                cookie_info,
            )

        return _parse_response(response)

    def delete(self, endpoint: str, **kwargs: Any) -> Any:
        """
        Make an authenticated DELETE request.

        Args:
            endpoint: API endpoint
            **kwargs: Additional arguments to pass to requests

        Returns:
            Parsed JSON response
        """
        url = self._make_url(endpoint)
        headers = self._get_headers()
        headers.update(kwargs.pop("headers", {}))

        response = self.session.delete(
            url,
            headers=headers,
            timeout=kwargs.pop("timeout", self.timeout),
            verify=self.verify_ssl,
            **kwargs,
        )

        return _parse_response(response)
