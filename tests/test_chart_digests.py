# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/lib/chart_digests.py, the chart-smoke digest check."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from chart_digests import check_digests, main

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
PINNED = f"ghcr.io/example/gerrit@{DIGEST}"
SCRIPT = Path(__file__).parent.parent / "scripts" / "lib" / "chart_digests.py"
CHART = Path(__file__).parent.parent / "K8S" / "charts" / "gerrit"

STATEFULSET = f"""\
---
# Source: gerrit/templates/statefulset.yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gerrit
spec:
  template:
    spec:
      initContainers:
        - name: init
          image: {PINNED}
      containers:
        - name: gerrit
          image: "{PINNED}"
"""


def _stream(*documents: str) -> str:
    return "\n---\n".join(documents)


def test_pinned_statefulset_passes() -> None:
    assert check_digests(STATEFULSET, DIGEST) == []


def test_flow_style_container_without_digest_fails() -> None:
    text = f"""\
kind: Deployment
metadata: {{name: web}}
spec:
  template:
    spec:
      containers:
        - name: gerrit
          image: {PINNED}
        - {{name: helper, image: "busybox:1"}}
"""
    problems = check_digests(text, DIGEST)
    assert len(problems) == 1
    assert "busybox:1" in problems[0]
    assert "Deployment/web" in problems[0]


def test_flow_style_pod_spec_is_parsed() -> None:
    text = (
        "{kind: Pod, metadata: {name: p}, spec: {containers: "
        f'[{{name: a, image: "{PINNED}"}}, {{name: b, "image": nginx}}]}}}}'
    )
    problems = check_digests(text, DIGEST)
    assert problems == [f"Pod/p.spec.containers[1]: nginx is not pinned to @{DIGEST}"]


def test_quoted_key_and_block_scalar_image_are_checked() -> None:
    text = """\
kind: Pod
metadata:
  name: p
spec:
  containers:
    - name: a
      "image": >-
        busybox:1
"""
    assert len(check_digests(text, DIGEST)) == 1


def test_init_container_without_digest_fails() -> None:
    text = STATEFULSET.replace(f"image: {PINNED}", "image: busybox:1", 1)
    problems = check_digests(text, DIGEST)
    assert problems == [
        "StatefulSet/gerrit.spec.template.spec.initContainers[0]: "
        f"busybox:1 is not pinned to @{DIGEST}"
    ]


def test_ephemeral_container_without_digest_fails() -> None:
    text = f"""\
kind: Pod
metadata: {{name: p}}
spec:
  containers: [{{name: a, image: "{PINNED}"}}]
  ephemeralContainers: [{{name: debug, image: busybox}}]
"""
    assert len(check_digests(text, DIGEST)) == 1


def test_cronjob_without_digest_fails() -> None:
    text = """\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: gc
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          initContainers:
            - name: wait
              image: busybox:1
          containers:
            - name: gc
              image: ghcr.io/example/gerrit:3.10
"""
    problems = check_digests(text, DIGEST)
    assert len(problems) == 2
    assert all(p.startswith("CronJob/gc.spec.jobTemplate.") for p in problems)


def test_pinned_cronjob_and_job_pass() -> None:
    cronjob = f"""\
kind: CronJob
metadata: {{name: gc}}
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers: [{{name: gc, image: "{PINNED}"}}]
"""
    job = f"""\
kind: Job
metadata: {{name: once}}
spec:
  template:
    spec:
      initContainers: [{{name: i, image: "{PINNED}"}}]
      containers: [{{name: c, image: "{PINNED}"}}]
"""
    assert check_digests(_stream(cronjob, job), DIGEST) == []


def test_wrong_digest_fails() -> None:
    assert len(check_digests(STATEFULSET, OTHER)) == 2


def test_digest_not_at_end_fails() -> None:
    text = STATEFULSET.replace(PINNED, f"{PINNED}-suffix")
    assert len(check_digests(text, DIGEST)) == 2


def test_container_without_image_fails() -> None:
    text = "kind: Pod\nmetadata: {name: p}\nspec:\n  containers: [{name: a}]\n"
    assert check_digests(text, DIGEST) == ["Pod/p.spec.containers[0]: no image"]


def test_no_containers_fails() -> None:
    text = "kind: ConfigMap\nmetadata: {name: c}\ndata: {image: busybox}\n"
    assert check_digests(text, DIGEST) == ["no containers found"]


def test_empty_documents_are_skipped() -> None:
    assert check_digests(f"---\n---\n{STATEFULSET}---\n", DIGEST) == []


def test_main_exit_codes(tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(STATEFULSET, encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(STATEFULSET.replace(PINNED, "busybox:1"), encoding="utf-8")
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: [unclosed\n", encoding="utf-8")

    assert main(["--digest", DIGEST, str(good)]) == 0
    assert main(["--digest", DIGEST, str(good), str(bad)]) == 1
    assert main(["--digest", DIGEST, str(broken)]) == 2
    assert main(["--digest", DIGEST, str(tmp_path / "missing.yaml")]) == 2
    assert main(["--digest", "sha256:short", str(good)]) == 2


def test_script_runs_standalone(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(STATEFULSET.replace(PINNED, "busybox:1"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--digest", DIGEST, str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "busybox:1 is not pinned" in result.stdout


def _helm_template(*args: str) -> str:
    return subprocess.run(
        ["helm", "template", "gerrit", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_rendered_chart() -> None:
    assert (
        check_digests(_helm_template("--set", f"image.digest={DIGEST}"), DIGEST) == []
    )
    assert check_digests(_helm_template(), DIGEST)
