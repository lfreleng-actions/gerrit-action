# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Container-free checks that exercise ``verify-tunnel.py``.

These checks drive the tunnel verification script as a subprocess and
assert on its *observable contract* — exit code and diagnostic output —
rather than importing it.  Running it out-of-process is deliberate: it
is what a workflow step does, so the checks cover the real entry point
including its environment-variable handling.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from harness_results import CheckResult

logger = logging.getLogger(__name__)


def run_tunnel_tests(tunnel_script: Path) -> list[CheckResult]:
    """Run the tunnel verification script tests that don't need a container."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("TUNNEL VERIFICATION TESTS (no container needed)")
    logger.info("=" * 60)
    logger.info("")

    results: list[CheckResult] = []
    results.append(check_tunnel_script_validates_inputs(tunnel_script))
    results.append(check_tunnel_script_handles_unreachable(tunnel_script))
    return results


def check_tunnel_script_validates_inputs(tunnel_script: Path) -> CheckResult:
    """Verify the tunnel script rejects missing env vars gracefully."""
    if not tunnel_script.exists():
        return CheckResult(
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
        return CheckResult(
            name="tunnel_input_validation",
            passed=False,
            message="script timed out",
        )

    if proc.returncode == 1 and "BORE_HOST" in (proc.stdout + proc.stderr):
        return CheckResult(
            name="tunnel_input_validation",
            passed=True,
            message="correctly rejects empty BORE_HOST with helpful error",
        )
    return CheckResult(
        name="tunnel_input_validation",
        passed=False,
        message=f"exit={proc.returncode} stderr={proc.stderr[:200]}",
    )


def check_tunnel_script_handles_unreachable(tunnel_script: Path) -> CheckResult:
    """Verify the tunnel script produces diagnostics for a bad host."""
    if not tunnel_script.exists():
        return CheckResult(
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
        return CheckResult(
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
        return CheckResult(
            name="tunnel_unreachable_diagnostics",
            passed=True,
            message="produced actionable diagnostic output on connection failure",
        )
    return CheckResult(
        name="tunnel_unreachable_diagnostics",
        passed=False,
        message=f"exit={proc.returncode} diagnostics_found={has_diagnostics}",
    )
