# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tunnel verification checks that need no Gerrit container.

Split out of ``scripts/test-replication-local.py``.  These drive
``scripts/verify-tunnel.py`` as a subprocess to confirm it validates its
inputs and emits actionable diagnostics when the far end is
unreachable.  Every name here is re-exported from the harness entry
point.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from replharness_model import SCRIPTS_DIR, TestResult

logger = logging.getLogger(__name__)


def _test_tunnel_script_validates_inputs() -> TestResult:
    """Verify the tunnel script rejects missing env vars gracefully."""
    tunnel_script = SCRIPTS_DIR / "verify-tunnel.py"
    if not tunnel_script.exists():
        return TestResult(
            name="tunnel_input_validation",
            passed=False,
            message="verify-tunnel.py not found",
        )

    # Run with empty BORE_HOST — should exit 1 with a helpful message
    env = os.environ.copy()
    env["BORE_HOST"] = ""
    env["HTTP_PORT"] = "8080"
    env.pop("GITHUB_STEP_SUMMARY", None)

    try:
        proc = subprocess.run(
            [sys.executable, str(tunnel_script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return TestResult(
            name="tunnel_input_validation",
            passed=False,
            message="script timed out",
        )

    if proc.returncode == 1 and "BORE_HOST" in (proc.stdout + proc.stderr):
        return TestResult(
            name="tunnel_input_validation",
            passed=True,
            message="correctly rejects empty BORE_HOST with helpful error",
        )
    return TestResult(
        name="tunnel_input_validation",
        passed=False,
        message=f"exit={proc.returncode} stderr={proc.stderr[:200]}",
    )


def _test_tunnel_script_handles_unreachable() -> TestResult:
    """Verify the tunnel script produces diagnostics for a bad host."""
    tunnel_script = SCRIPTS_DIR / "verify-tunnel.py"
    if not tunnel_script.exists():
        return TestResult(
            name="tunnel_unreachable_diagnostics",
            passed=False,
            message="verify-tunnel.py not found",
        )

    env = os.environ.copy()
    env["BORE_HOST"] = "192.0.2.1"  # RFC 5737 TEST-NET — guaranteed unreachable
    env["HTTP_PORT"] = "1"
    env["MAX_ATTEMPTS"] = "1"
    env["RETRY_DELAY"] = "0"
    env["DEBUG"] = "true"
    env.pop("GITHUB_STEP_SUMMARY", None)

    try:
        proc = subprocess.run(
            [sys.executable, str(tunnel_script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return TestResult(
            name="tunnel_unreachable_diagnostics",
            passed=False,
            message="script timed out (expected quick failure)",
        )

    combined = proc.stdout + proc.stderr
    has_diagnostics = any(
        kw in combined
        for kw in (
            "Network diagnostics",
            "Possible causes",
            "error_type",
            "connection_refused",
            "timeout",
            "FAILED",
        )
    )

    if proc.returncode != 0 and has_diagnostics:
        return TestResult(
            name="tunnel_unreachable_diagnostics",
            passed=True,
            message="produced actionable diagnostic output on connection failure",
        )
    return TestResult(
        name="tunnel_unreachable_diagnostics",
        passed=False,
        message=f"exit={proc.returncode} diagnostics_found={has_diagnostics}",
    )


def run_tunnel_tests() -> list[TestResult]:
    """Run the tunnel verification script tests that don't need a container."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("TUNNEL VERIFICATION TESTS (no container needed)")
    logger.info("=" * 60)
    logger.info("")

    results: list[TestResult] = []
    results.append(_test_tunnel_script_validates_inputs())
    results.append(_test_tunnel_script_handles_unreachable())
    return results
