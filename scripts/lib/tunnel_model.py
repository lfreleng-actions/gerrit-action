# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Result record shared by the tunnel probe implementations.

Split out of ``verify-tunnel.py`` so the ``requests`` probe (which
stays in the entry point, next to the optional import it binds) and the
``urllib`` fallback in :mod:`tunnel_probe_urllib` describe their outcome
with the same type.  The name is re-exported from ``verify-tunnel.py``.
"""

from __future__ import annotations


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
