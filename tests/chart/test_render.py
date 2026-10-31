# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Golden tests for the configuration files the Gerrit chart renders.

Renders the chart with ``helm template`` and each values file under
``K8S/charts/gerrit/examples/``, and compares the ``<fullname>-config``
ConfigMap data with ``tests/chart/golden/<example>/<file>``. Each golden
starts with the two SPDX comment lines, which the comparison skips.
Also checks that the NetworkPolicies let the configured mail relay
through.

After an intended change to the rendered output, regenerate the
goldens with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
the diff.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "K8S" / "charts" / "gerrit"
EXAMPLES = CHART / "examples"
GOLDEN = Path(__file__).parent / "golden"
CONFIG_FILES = ("gerrit.config", "jgit.config", "notedb.config")
# REUSE-IgnoreStart
GOLDEN_HEADER = (
    "# SPDX-License-Identifier: Apache-2.0\n"
    "# SPDX-FileCopyrightText: 2026 The Linux Foundation\n"
)
# REUSE-IgnoreEnd
EXAMPLE_NAMES = sorted(p.name for p in GOLDEN.iterdir() if p.is_dir())

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def helm(*args: str) -> str:
    """Run helm and return its standard output, failing on an error."""
    proc = subprocess.run(
        ["helm", *args], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def render_config(values: Path) -> dict[str, str]:
    """Return the data of the rendered gerrit-config ConfigMap."""
    out = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        "-f",
        str(values),
        "--show-only",
        "templates/configmaps.yaml",
    )
    docs: list[Any] = [d for d in yaml.safe_load_all(out) if d]
    found = [d for d in docs if d["metadata"]["name"] == "gerrit-config"]
    assert len(found) == 1, [d["metadata"]["name"] for d in docs]
    data: dict[str, str] = found[0]["data"]
    return data


def egress_ports(*args: str) -> set[int]:
    """Return the TCP egress ports the chart's NetworkPolicies allow."""
    out = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        *args,
        "--show-only",
        "templates/networkpolicies.yaml",
    )
    return {
        port["port"]
        for doc in yaml.safe_load_all(out)
        if doc
        for rule in doc["spec"].get("egress", [])
        for port in rule.get("ports", [])
        if port["protocol"] == "TCP"
    }


def read_golden(path: Path) -> str:
    """Return a golden file's content without its SPDX header."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith(GOLDEN_HEADER), f"{path} lacks the SPDX header"
    return text[len(GOLDEN_HEADER) :]


def test_every_example_has_goldens() -> None:
    examples = sorted(p.stem for p in EXAMPLES.glob("*.yaml"))
    assert examples == EXAMPLE_NAMES
    assert len(examples) >= 3


@pytest.mark.xfail(strict=True, reason="the chart renders no gerrit.config yet")
@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_rendered_config_matches_golden(example: str) -> None:
    data = render_config(EXAMPLES / f"{example}.yaml")
    assert sorted(data) == sorted(CONFIG_FILES)
    for name in CONFIG_FILES:
        golden = GOLDEN / example / name
        if os.environ.get("UPDATE_GOLDEN"):
            golden.write_text(GOLDEN_HEADER + data[name], encoding="utf-8")
        assert data[name] == read_golden(golden), f"{example}/{name}"


@pytest.mark.xfail(strict=True, reason="the chart renders no gerrit.config yet")
@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_rendered_config_parses_as_git_config(example: str, tmp_path: Path) -> None:
    data = render_config(EXAMPLES / f"{example}.yaml")
    for name in CONFIG_FILES:
        path = tmp_path / name
        path.write_text(data[name], encoding="utf-8")
        proc = subprocess.run(
            ["git", "config", "-f", str(path), "--list"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{example}/{name}: {proc.stderr}"


@pytest.mark.xfail(strict=True, reason="the chart renders no gerrit.config yet")
def test_secrets_stay_out_of_the_rendered_config() -> None:
    data = render_config(EXAMPLES / "values-one-segment-prefix.yaml")
    config = data["gerrit.config"].lower()
    assert "registeremailprivatekey" not in config
    assert "smtppass" not in config


def test_no_smtp_egress_without_mail() -> None:
    assert egress_ports() == {53, 443}


@pytest.mark.xfail(strict=True, reason="the chart opens no SMTP egress yet")
def test_smtp_egress_uses_the_configured_port() -> None:
    ports = egress_ports("-f", str(EXAMPLES / "values-one-segment-prefix.yaml"))
    assert 587 in ports


@pytest.mark.xfail(strict=True, reason="the chart opens no SMTP egress yet")
@pytest.mark.parametrize(("encryption", "port"), [("none", 25), ("ssl", 465)])
def test_smtp_egress_defaults_to_gerrits_port(encryption: str, port: int) -> None:
    ports = egress_ports(
        "--set",
        "sendemail.enable=true",
        "--set",
        "sendemail.smtpServer=smtp.example.org",
        "--set",
        f"sendemail.smtpEncryption={encryption}",
    )
    assert port in ports


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_helm_lint(example: str) -> None:
    helm("lint", str(CHART), "-f", str(EXAMPLES / f"{example}.yaml"))
