# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Guard against credential-shaped literals entering the repository.

Placeholder credentials in tests and documentation are harmless in
themselves, but they trip GitHub secret scanning and every comparable
tool, producing alerts that have to be triaged and dismissed by hand
for as long as the literal survives in history.  Keeping them out is
cheaper than dismissing them.

Every pattern below is assembled from fragments so that this module
does not match itself; no allowlist is needed, and none should be
added.  The guard scans this file along with everything else, so any
sample string used to exercise a pattern must be assembled the same
way.  If a new placeholder is required, express it through
:mod:`credential_stubs` or use an obviously non-credential form such
as ``<github-pat>``.

The patterns mirror what scanners actually alert on, and nothing
wider.  ``BEGIN CERTIFICATE`` is deliberately absent: X.509
certificates are public material that no scanner flags, so guarding
it would block a legitimate checked-in CA bundle for no benefit.
With no allowlist to fall back on, a pattern that over-reaches has
no escape hatch, so each one has to earn its place.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
"""Repository root, derived from this file's location."""

_D = "-" * 5
_COLON = ":"
_AT = "@"

CREDENTIAL_PATTERNS: dict[str, str] = {
    "PEM private key armour": rf"{_D}BEGIN [A-Z0-9 ]*PRIVATE KEY{_D}",
    "PGP private key block": rf"{_D}BEGIN PGP PRIVATE KEY BLOCK{_D}",
    "PuTTY private key": "PuTTY" + "-User-Key-File",
    "GitHub personal access token": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "GitHub fine-grained token": r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}",
    "GitLab personal access token": r"glpat-[0-9A-Za-z_-]{20,}",
    "AWS access key id": r"(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}",
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "Slack token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "PyPI upload token": r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{70,}",
    "Docker Hub access token": (
        r"\bdckr_(?:pat_[A-Za-z0-9_-]{27}|oat_[A-Za-z0-9_-]{32})"
        r"(?:[^A-Za-z0-9_-]|$)"
    ),
    "URL with inline credentials": r"(?i)[a-z][a-z0-9+.-]*://[^/\s:@\"']+:[^/\s@\"']+@",
}
"""Patterns that secret scanners alert on, keyed by human-readable name."""

_COMPILED = {name: re.compile(rx) for name, rx in CREDENTIAL_PATTERNS.items()}


def _tracked_files() -> list[Path]:
    """Return every file tracked by git.

    Skips only when the git executable is genuinely absent.  A failed
    or timed-out enumeration is deliberately allowed to propagate: a
    guard that reports "skipped" on error passes CI having scanned
    nothing, which is the one failure mode it must never have.

    Returns
    -------
    list[Path]
        Absolute paths to tracked files.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except FileNotFoundError:
        pytest.skip("git executable not found; cannot enumerate tracked files")
    names = result.stdout.decode("utf-8").split("\0")
    paths = [REPO_ROOT / name for name in names if name]
    assert paths, "git ls-files reported no tracked files; the guard scanned nothing"
    return paths


def _readable_text(path: Path) -> str | None:
    """Return the text of *path*, or None when it is binary or missing.

    Parameters
    ----------
    path:
        File to read.

    Returns
    -------
    str | None
        Decoded contents, or None when the file should not be scanned.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def test_no_credential_shaped_literals() -> None:
    """Fail when any tracked file carries a scanner-triggering literal."""
    findings: list[str] = []
    for path in _tracked_files():
        text = _readable_text(path)
        if text is None:
            continue
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, pattern in _COMPILED.items():
                if pattern.search(line):
                    findings.append(f"{rel}:{lineno}: {name}")

    assert not findings, (
        "Credential-shaped literals found; these trigger secret scanning.\n"
        "Use credential_stubs.pem_block()/pem_header() for PEM armour, or an\n"
        "obviously non-credential placeholder such as <github-pat>.\n\n"
        + "\n".join(findings)
    )


def test_patterns_match_realistic_examples() -> None:
    """Confirm the guard's patterns still match what they describe.

    Without this the suite could pass because the patterns rotted
    rather than because the repository is clean.
    """
    samples = {
        "PEM private key armour": f"{_D}BEGIN OPENSSH PRIVATE KEY{_D}",
        "PGP private key block": f"{_D}BEGIN PGP PRIVATE KEY BLOCK{_D}",
        "PuTTY private key": "PuTTY" + "-User-Key-File: ssh-ed25519",
        "GitHub personal access token": "ghp_" + "a" * 36,
        "GitHub fine-grained token": "github_pat_" + "b" * 22 + "_" + "c" * 59,
        "GitLab personal access token": "glpat-" + "g" * 20,
        "AWS access key id": "AKIA" + "C" * 16,
        "Google API key": "AIza" + "d" * 35,
        "Slack token": "xoxb-" + "1" * 12,
        "PyPI upload token": "pypi-AgEIcHlwaS5vcmc" + "e" * 70,
        "Docker Hub access token": "dckr_pat_" + "f" * 27,
        "URL with inline credentials": f"https://user{_COLON}pass{_AT}example.com/repo",
    }
    assert set(samples) == set(_COMPILED), "sample set drifted from pattern set"
    for name, sample in samples.items():
        assert _COMPILED[name].search(sample), f"{name} no longer matches its sample"

    # Schemes are case-insensitive per RFC 3986, and a colon is legal
    # inside the password half of the userinfo component.
    awkward_url = f"HTTPS://user{_COLON}pa{_COLON}ss{_AT}example.com/repo"
    inline_creds = _COMPILED["URL with inline credentials"]
    assert inline_creds.search(awkward_url), (
        "uppercase scheme or colon in password evades the guard"
    )

    # Docker issues organisation tokens alongside personal ones, and
    # GitHub secret scanning flags both.  The two carry different
    # suffix lengths, mirroring TruffleHog's DockerHub v2 detector.
    org_token = "dckr_oat_" + "f" * 32
    assert _COMPILED["Docker Hub access token"].search(org_token), (
        "Docker organisation access tokens evade the guard"
    )


def test_short_placeholders_are_permitted() -> None:
    """Confirm obviously-fake short tokens do not fail the guard.

    Scanners require a full-length token before alerting, so the suite
    keeps its readable short fixtures such as ``ghp_testtoken123``.
    """
    for benign in (
        "ghp_testtoken123",
        "ghp_tok",
        "github_pat_zzzz",
        "<github-pat>",
        # An organisation token is 32 characters; a 27-character one is
        # a truncated placeholder that no scanner would alert on.
        "dckr_oat_" + "f" * 27,
    ):
        matched = [name for name, rx in _COMPILED.items() if rx.search(benign)]
        assert not matched, f"{benign} unexpectedly matched {matched}"
