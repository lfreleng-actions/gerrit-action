# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Gerrit REST wire-protocol primitives.

Owns everything that is true of a Gerrit HTTP response regardless of
which endpoint produced it, so that no other module has to know how
Gerrit encodes its payloads or reports its failures:

* The error taxonomy (:class:`GerritAPIError` and subclasses) that the
  whole client raises and catches.
* Gerrit's magic JSON prefix and the response parser that strips it.
* Detection of the garbled-verb ``"Not implemented: <mangled>POST"``
  body Gerrit returns when a request line is corrupted on a reused
  keepalive connection.
* Parsing of outbound ``Cookie`` header values, used for auth
  diagnostics without ever logging cookie values.

This module performs no I/O: it only interprets
:class:`requests.Response` objects that a caller has already received.
"""

from __future__ import annotations

import json
from typing import Any

import requests

# Gerrit API constants
GERRIT_MAGIC_JSON_PREFIX = ")]}'\n"
DEFAULT_TIMEOUT = 30
DEFAULT_ADMIN_ACCOUNTS = [1000000, 1]  # Gerrit 3.x uses 1000000, older uses 1


class GerritAPIError(Exception):
    """Base exception for Gerrit API errors."""

    def __init__(
        self, message: str, status_code: int | None = None, response_text: str = ""
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class GerritAuthError(GerritAPIError):
    """Authentication failed."""


class GerritNotFoundError(GerritAPIError):
    """Resource not found."""


class GerritConflictError(GerritAPIError):
    """Resource already exists or conflict."""


def _cookie_names_from_header(cookie_header: str) -> set[str]:
    """Parse cookie names from a ``Cookie`` header value.

    Returns a set of cookie names (the part before ``=`` in each
    ``name=value`` pair separated by ``; ``).
    """
    names: set[str] = set()
    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if "=" in pair:
            names.add(pair.split("=", 1)[0])
    return names


def _strip_gerrit_prefix(content: str) -> str:
    """Strip Gerrit's magic JSON prefix from response content."""
    if content.startswith(GERRIT_MAGIC_JSON_PREFIX):
        return content[len(GERRIT_MAGIC_JSON_PREFIX) :]
    # Also handle without newline
    elif content.startswith(")]}'"):
        return content[4:]
    return content


def _looks_like_method_mangle(exc: GerritAPIError) -> bool:
    """Detect Gerrit's "Not implemented: <garbled>POST <uri>" response.

    Gerrit's :class:`RestApiServlet` formats unknown-HTTP-method errors
    as ``"Not implemented: <method> <uri>"``.  We have seen the method
    portion arrive corrupted (e.g. ``alPOST``, ``lPOST``) when several
    POSTs are issued in quick succession over a single keepalive TCP
    connection through certain proxy stacks.  The HTTP verb our client
    actually sends is correct; the corruption happens between the
    socket and the servlet.

    We use this helper to distinguish that benign, retry-friendly
    pattern from genuine API errors so we can suppress the noisy
    warning and try once on a fresh connection.

    Returns ``True`` only when the verb token is an *actually mangled*
    POST (it ends with ``POST`` but is not exactly ``POST``) and, when
    the status code is known, it is the ``405 Method Not Allowed`` that
    Gerrit returns for an unrecognised method.  This avoids masking a
    genuine ``405`` (e.g. an endpoint that truly does not accept POST),
    whose verb token is the clean ``POST``.
    """
    # A real "Not implemented" response uses HTTP 405; if we have a
    # status code and it is something else, this is not the mangle
    # pattern.
    if exc.status_code is not None and exc.status_code != 405:
        return False
    body = (exc.response_text or str(exc) or "").strip()
    if "Not implemented:" not in body:
        return False
    after = body.split("Not implemented:", 1)[1].strip()
    # Format is "<method> <uri>"; inspect only the verb token so a
    # clean "POST /..." (a genuine 405) is not treated as corruption.
    verb = after.split(None, 1)[0] if after else ""
    return verb.endswith("POST") and verb != "POST"


def _parse_response(response: requests.Response, allow_non_json: bool = False) -> Any:
    """Parse Gerrit API response, handling magic prefix and errors.

    Args:
        response: The requests Response object
        allow_non_json: If True, return raw text for non-JSON responses and
            JSON decode failures instead of raising an error.
    """
    content_type = response.headers.get("content-type", "")

    # Check for errors first
    if response.status_code == 401:
        raise GerritAuthError(
            "Authentication failed",
            status_code=response.status_code,
            response_text=response.text,
        )
    if response.status_code == 403:
        raise GerritAuthError(
            f"Permission denied: {response.text}",
            status_code=response.status_code,
            response_text=response.text,
        )
    if response.status_code == 404:
        raise GerritNotFoundError(
            "Resource not found",
            status_code=response.status_code,
            response_text=response.text,
        )
    if response.status_code == 409:
        raise GerritConflictError(
            f"Conflict: {response.text}",
            status_code=response.status_code,
            response_text=response.text,
        )
    if not response.ok:
        raise GerritAPIError(
            f"API request failed: {response.text}",
            status_code=response.status_code,
            response_text=response.text,
        )

    # Handle empty responses (common for successful PUT/POST/DELETE)
    if not response.content:
        return None

    content = response.text.strip()
    if not content:
        return None

    # Parse JSON if content type indicates JSON
    if "application/json" in content_type:
        content = _strip_gerrit_prefix(content)
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            if allow_non_json:
                return content
            raise GerritAPIError(
                f"Failed to parse JSON response: {e}",
                status_code=response.status_code,
                response_text=response.text,
            ) from e

    # For non-JSON responses, return raw content or raise
    if allow_non_json:
        return content
    raise GerritAPIError(
        f"Unexpected non-JSON content-type: {content_type}",
        status_code=response.status_code,
        response_text=response.text,
    )
