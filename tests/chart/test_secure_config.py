# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for render-config.sh, which writes etc/secure.config.

Runs the script against a scratch site with fixed environment values
and compares the file with ``tests/chart/golden/secure.config``, which
starts with the two SPDX comment lines the comparison skips.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "K8S" / "charts" / "gerrit" / "files" / "scripts" / "render-config.sh"
GOLDEN = Path(__file__).parent / "golden" / "secure.config"
# REUSE-IgnoreStart
GOLDEN_HEADER = (
    "# SPDX-License-Identifier: Apache-2.0\n"
    "# SPDX-FileCopyrightText: 2026 The Linux Foundation\n"
)
# REUSE-IgnoreEnd
SECRET_ENV = {
    "GERRIT_REGISTER_EMAIL_PRIVATE_KEY": "fixed-register-email-key",
    "GERRIT_SMTP_PASS": "fixed smtp pass; with # marks",
}

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("sh") is None,
    reason="needs sh and git",
)


def run_script(site: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run render-config.sh for the site with only the given secrets set."""
    base = {
        k: v for k, v in os.environ.items() if not k.startswith(("GERRIT_", "GIT_"))
    }
    return subprocess.run(
        ["sh", str(SCRIPT)],
        env={**base, "HOME": str(site), "GERRIT_SITE": str(site), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A scratch site directory with an etc/ directory."""
    (tmp_path / "etc").mkdir()
    return tmp_path


def test_secure_config_is_private(site: Path) -> None:
    proc = run_script(site, SECRET_ENV)
    assert proc.returncode == 0, proc.stderr
    mode = stat.S_IMODE((site / "etc" / "secure.config").stat().st_mode)
    assert mode == 0o600


def test_secure_config_matches_golden(site: Path) -> None:
    proc = run_script(site, SECRET_ENV)
    assert proc.returncode == 0, proc.stderr
    text = GOLDEN.read_text(encoding="utf-8")
    assert text.startswith(GOLDEN_HEADER)
    secure = (site / "etc" / "secure.config").read_text(encoding="utf-8")
    assert secure == text[len(GOLDEN_HEADER) :]


def test_empty_optional_secret_is_left_out(site: Path) -> None:
    env = {**SECRET_ENV, "GERRIT_SMTP_PASS": ""}
    proc = run_script(site, env)
    assert proc.returncode == 0, proc.stderr
    secure = (site / "etc" / "secure.config").read_text(encoding="utf-8")
    assert "smtpPass" not in secure
    assert "registerEmailPrivateKey" in secure


def test_missing_register_email_key_fails(site: Path) -> None:
    proc = run_script(site, {})
    assert proc.returncode != 0
    assert not (site / "etc" / "secure.config").exists()
