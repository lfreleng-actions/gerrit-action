# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""HTTP probe outcome type and the dependency-free fallback probe.

This module owns the *vocabulary* of a single tunnel probe
(:class:`TunnelCheckResult`) plus the ``urllib``-based implementation
used when the ``requests`` library is unavailable.

The ``requests``-based probe deliberately stays in
``scripts/verify-tunnel.py``: it binds to that script's module-level
``requests`` import and ``_HAS_REQUESTS`` capability flag, which the
test-suite patches directly.  Keeping the stdlib fallback here lets the
entry-point script stay small without moving any patched symbol.
"""

from __future__ import annotations

import contextlib
import time
import urllib.error
import urllib.request


class TunnelCheckResult:
    """Outcome of a single HTTP probe against the tunnel."""

    def __init__(
        self,
        *,
        success: bool = False,
        status_code: int | None = None,
        body: str = "",
        error: str = "",
        error_type: str = "",
        elapsed_ms: float = 0.0,
    ) -> None:
        self.success = success
        self.status_code = status_code
        self.body = body
        self.error = error
        self.error_type = error_type
        self.elapsed_ms = elapsed_ms

    def __repr__(self) -> str:
        if self.success:
            return f"<OK status={self.status_code} {self.elapsed_ms:.0f}ms>"
        return f"<FAIL type={self.error_type} status={self.status_code} error={self.error!r}>"


def probe_with_urllib(url: str, timeout: float = 10.0) -> TunnelCheckResult:
    """Probe *url* using the stdlib ``urllib`` (fallback)."""
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = (time.monotonic() - start) * 1000
            body = resp.read().decode("utf-8", errors="replace").strip()
            status = resp.getcode()
            if status == 200:
                return TunnelCheckResult(
                    success=True,
                    status_code=status,
                    body=body,
                    elapsed_ms=elapsed,
                )
            return TunnelCheckResult(
                success=False,
                status_code=status,
                body=body[:500],
                error=f"HTTP {status}",
                error_type="http_error",
                elapsed_ms=elapsed,
            )

    except urllib.error.HTTPError as exc:
        elapsed = (time.monotonic() - start) * 1000
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace")[:500]
        return TunnelCheckResult(
            success=False,
            status_code=exc.code,
            body=body,
            error=f"HTTP {exc.code} {exc.reason}",
            error_type="http_error",
            elapsed_ms=elapsed,
        )

    except urllib.error.URLError as exc:
        elapsed = (time.monotonic() - start) * 1000
        reason = str(exc.reason)
        if "Connection refused" in reason:
            error_type = "connection_refused"
        elif "Name or service not known" in reason or "getaddrinfo" in reason:
            error_type = "dns_failure"
        elif "timed out" in reason:
            error_type = "timeout"
        else:
            error_type = "connection_error"
        return TunnelCheckResult(
            success=False,
            error=reason[:300],
            error_type=error_type,
            elapsed_ms=elapsed,
        )

    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return TunnelCheckResult(
            success=False,
            error=str(exc)[:300],
            error_type="unexpected",
            elapsed_ms=elapsed,
        )
