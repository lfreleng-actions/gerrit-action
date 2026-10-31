# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's git gc and cache maintenance.

Checks the ``[gc]``, ``[cache "<name>"]`` and ``receive.autogc``
settings the chart renders, compares the optional git gc CronJob with
``tests/chart/golden/gc-cronjob.golden``, and runs
``files/scripts/git-gc.sh`` against a fixture git store.

After an intended change to the rendered CronJob, regenerate the
golden with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
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
from chart.test_render import CHART, GOLDEN, GOLDEN_HEADER, helm, read_golden

CACHE_LIMITS = (
    "comment_context",
    "conflicts",
    "diff_intraline",
    "diff_summary",
    "gerrit_file_diff",
    "git_file_diff",
    "git_modified_files",
    "modified_files",
)
CRONJOB = ("--set", "gc.cronJob.enabled=true")
GC_SCRIPT = CHART / "files" / "scripts" / "git-gc.sh"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def render(*args: str) -> list[dict[str, Any]]:
    """Return every object the chart renders with the given helm options."""
    out = helm("template", "gerrit", str(CHART), "--namespace", "gerrit-test", *args)
    return [d for d in yaml.safe_load_all(out) if d]


def config_file(docs: list[dict[str, Any]], name: str) -> str:
    """Return one file of the rendered gerrit-config ConfigMap."""
    found = [
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "gerrit-config"
    ]
    assert len(found) == 1
    data: str = found[0]["data"][name]
    return data


def git_config(text: str, tmp_path: Path, *args: str) -> str:
    """Return ``git config`` output for a rendered config file."""
    path = tmp_path / "rendered.config"
    path.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        ["git", "config", "-f", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def cronjobs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the rendered CronJobs."""
    return [d for d in docs if d["kind"] == "CronJob"]


@needs_helm
def test_gc_scheduler_and_autogc_defaults(tmp_path: Path) -> None:
    docs = render()
    config = config_file(docs, "gerrit.config")
    assert git_config(config, tmp_path, "gc.startTime")
    assert git_config(config, tmp_path, "gc.interval") == "1 week"
    jgit = config_file(docs, "jgit.config")
    assert git_config(jgit, tmp_path, "--bool", "receive.autogc") == "false"


@needs_helm
def test_cache_limits_render_by_default(tmp_path: Path) -> None:
    config = config_file(render(), "gerrit.config")
    for name in CACHE_LIMITS:
        assert git_config(config, tmp_path, f"cache.{name}.diskLimit"), name
    assert '[cache "limits"]' not in config


@needs_helm
def test_cache_limits_override(tmp_path: Path) -> None:
    config = config_file(
        render(
            "--set",
            "cache.limits.diff_intraline=2g",
            "--set",
            "cache.limits.conflicts=",
            "--set",
            "cache.git_file_diff.diskLimit=4g",
        ),
        "gerrit.config",
    )
    assert git_config(config, tmp_path, "cache.diff_intraline.diskLimit") == "2g"
    assert git_config(config, tmp_path, "cache.conflicts.diskLimit") == ""
    assert git_config(config, tmp_path, "cache.git_file_diff.diskLimit") == "4g"


@needs_helm
def test_no_cronjob_by_default() -> None:
    assert cronjobs(render()) == []


@needs_helm
def test_cronjob_matches_golden() -> None:
    out = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        *CRONJOB,
        "--show-only",
        "templates/gc-cronjob.yaml",
    )
    golden = GOLDEN / "gc-cronjob.golden"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + out, encoding="utf-8")
    assert out == read_golden(golden)


@needs_helm
def test_cronjob_turns_off_the_builtin_scheduler(tmp_path: Path) -> None:
    config = config_file(render(*CRONJOB), "gerrit.config")
    assert git_config(config, tmp_path, "gc.startTime") == ""
    assert git_config(config, tmp_path, "gc.interval") == ""
    assert '[gc "cronJob"]' not in config


@needs_helm
def test_cronjob_pod_is_isolated() -> None:
    docs = render(*CRONJOB)
    (cron,) = cronjobs(docs)
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsUser"] == 1001
    claims = [
        v["persistentVolumeClaim"]
        for v in spec["volumes"]
        if "persistentVolumeClaim" in v
    ]
    assert claims == [{"claimName": "site-gerrit-0"}]
    labels = pod["metadata"]["labels"]
    for svc in (d for d in docs if d["kind"] == "Service"):
        selector = svc["spec"]["selector"].items()
        assert not selector <= labels.items(), svc["metadata"]["name"]
    policies = [
        d
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
    ]
    assert policies
    assert all("ingress" not in p["spec"] for p in policies)
    assert all("egress" not in p["spec"] for p in policies)


def make_repo(store: Path, name: str) -> Path:
    """Create a bare repository with loose objects under the store."""
    work = store.parent / "work" / name
    work.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.org",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.org",
    }
    subprocess.run(["git", "init", "-q", str(work)], check=True, env=env)
    for i in range(3):
        (work / f"file{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(work), "add", "."], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(work), "commit", "-q", "-m", f"Commit {i}"],
            check=True,
            env=env,
        )
    bare = store / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", str(bare), "HEAD:refs/heads/main"],
        check=True,
        env=env,
    )
    return bare


def loose_objects(repo: Path) -> list[Path]:
    """Return the loose object files of a repository."""
    return [p for p in (repo / "objects").glob("??/*") if p.is_file()]


def packs(repo: Path) -> list[Path]:
    """Return the pack files of a repository."""
    return list((repo / "objects" / "pack").glob("*.pack"))


def run_gc(
    site: Path,
    *gc_args: str,
    prefix: tuple[str, ...] = (),
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git-gc.sh on a site; with no gc_args, pack everything."""
    env = {
        **os.environ,
        "GERRIT_SITE": str(site),
        "HOME": str(site),
        "TMPDIR": str(site),
    }
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [*prefix, "sh", str(GC_SCRIPT), *(gc_args or ("--prune=now",))],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_gc_script_packs_every_repository(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    store = site / "git"
    repos = [make_repo(store, "All-Projects"), make_repo(store, "team/project")]
    for repo in repos:
        assert loose_objects(repo)
    proc = run_gc(site)
    assert proc.returncode == 0, proc.stderr
    for repo in repos:
        assert packs(repo), repo
        assert loose_objects(repo) == [], repo


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock")
def test_gc_script_skips_a_locked_repository(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    repo = make_repo(site / "git", "locked")
    proc = run_gc(site, prefix=("flock", str(repo / "chart-gc.lock")))
    assert proc.returncode == 0, proc.stderr
    assert "locked" in proc.stdout
    assert packs(repo) == []
    assert loose_objects(repo)


def test_gc_script_fails_on_a_broken_repository(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    good = make_repo(site / "git", "good")
    (site / "git" / "broken.git").mkdir()
    proc = run_gc(site)
    assert proc.returncode != 0
    assert "broken.git" in proc.stderr
    assert packs(good)


def test_gc_script_keeps_argument_boundaries(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    repo = make_repo(site / "git", "spaced")
    proc = run_gc(site, "--prune=1 second ago")
    assert proc.returncode == 0, proc.stderr
    assert packs(repo)


def test_gc_script_waits_for_auto_gc(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    make_repo(site / "git", "auto")
    real_git = shutil.which("git")
    assert real_git
    log = tmp_path / "git-args.log"
    shim = tmp_path / "bin" / "git"
    shim.parent.mkdir()
    shim.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" -- >> '{log}'\nexec '{real_git}' \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    proc = run_gc(site, "--auto", path=f"{shim.parent}{os.pathsep}{os.environ['PATH']}")
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8").split("--\n")
    (gc,) = [c.splitlines() for c in calls if "gc" in c.splitlines()]
    assert "gc.autoDetach=false" in gc[: gc.index("gc")]
    assert gc[gc.index("gc") + 1 :] == ["--auto"]


@needs_helm
def test_cronjob_passes_args_as_container_args() -> None:
    (cron,) = cronjobs(
        render(*CRONJOB, "--set-json", 'gc.cronJob.args=["--prune=2 weeks ago"]')
    )
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["args"] == ["--prune=2 weeks ago"]
    assert "GC_ARGS" not in {e["name"] for e in container.get("env", [])}


@needs_helm
def test_cronjob_requires_the_gerrit_node() -> None:
    docs = render(*CRONJOB)
    (cron,) = cronjobs(docs)
    affinity = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["affinity"]
    (term,) = affinity["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert term["topologyKey"] == "kubernetes.io/hostname"
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    assert (
        term["labelSelector"]["matchLabels"] == sts["spec"]["selector"]["matchLabels"]
    )


@needs_helm
@pytest.mark.parametrize(
    "override",
    [("--set", "nameOverride=" + "n" * 63), ("--set", "fullnameOverride=" + "f" * 52)],
)
def test_cronjob_names_fit_kubernetes_limits(override: tuple[str, str]) -> None:
    docs = render(*CRONJOB, *override)
    (cron,) = cronjobs(docs)
    assert len(cron["metadata"]["name"]) <= 52
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    gerrit_name = sts["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"]
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]
    for obj in (cron, pod):
        for value in obj["metadata"]["labels"].values():
            assert len(value) <= 63, value
    assert pod["metadata"]["labels"]["app.kubernetes.io/name"] != gerrit_name
