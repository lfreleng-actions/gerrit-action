# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Container-deployment phases of the ``configure-g2p`` pipeline.

Each function here is one numbered step of the G2P configuration
run: validating the parsed config, running the GitHub-side checks,
loading the instance metadata written by ``start-instances.py``, and
configuring plus self-testing every running container.  Splitting
them out keeps the entry point a readable list of phases and lets
each phase be exercised on its own.

Usage::

    from g2p_deploy import configure_instances, load_running_instances

    instances = load_running_instances(action_config)
    setup_results, reports = configure_instances(cfg, docker, instances)
"""

from __future__ import annotations

import logging
from typing import Any

from config import ActionConfig, InstanceStore
from docker_manager import DockerManager
from errors import G2PCheckError, G2PConfigError
from g2p_config import G2PConfig
from g2p_github import (
    G2PCheckResult,
    check_github_config,
    format_check_results,
    results_to_json,
)
from g2p_setup import (
    G2PSelfTestReport,
    G2PSetupResult,
    selftest_g2p_plumbing,
    setup_g2p,
)
from logging_utils import log_group

logger = logging.getLogger(__name__)


def validate_g2p_config(config: G2PConfig) -> None:
    """Validate the parsed G2P config, aborting on fatal errors.

    Raises
    ------
    G2PConfigError
        When :meth:`G2PConfig.check` reports one or more errors.
    """
    with log_group("G2P configuration validation"):
        errors = config.check()
        if errors:
            for err in errors:
                # logger.error routes through _GitHubActionsFormatter
                # which emits the ::error:: annotation exactly once.
                logger.error("G2P config error: %s", err)
            raise G2PConfigError(f"G2P configuration has {len(errors)} error(s)")
        logger.info("G2P configuration valid ✅")


def run_github_checks(config: G2PConfig) -> tuple[list[G2PCheckResult], str]:
    """Run the generic GitHub-side checks for the target org.

    These are the token, org access, ``.github`` magic repo and
    workflow presence checks.  Org-level secret and variable audits
    are performed separately (see :mod:`g2p_org_provision`) so that
    provisioning, when enabled, happens *before* the final audit.

    Returns
    -------
    tuple[list[G2PCheckResult], str]
        The individual results and their JSON serialisation.

    Raises
    ------
    G2PCheckError
        When a check fails fatally in strict validation mode.
    """
    check_json = "[]"
    check_results: list[G2PCheckResult] = []
    if config.validation_mode == "skip":
        logger.info("GitHub checks skipped (validation_mode=skip)")
        return check_results, check_json

    with log_group("G2P GitHub checks"):
        check_results = check_github_config(config)
        check_json = results_to_json(check_results)

        # format_check_results logs warnings/errors through the
        # standard logger (which emits ::warning::/::error::
        # annotations exactly once); we do not re-print the
        # returned annotation strings.
        _, has_fatal = format_check_results(check_results, config.validation_mode)

        if has_fatal:
            raise G2PCheckError(
                "GitHub-side checks failed in strict mode",
                failed_checks=[
                    r.check_name
                    for r in check_results
                    if not r.passed and r.severity == "error"
                ],
            )

        passed = sum(1 for r in check_results if r.passed)
        total = len(check_results)
        logger.info("GitHub checks: %d/%d passed ✅", passed, total)

    return check_results, check_json


def load_running_instances(
    action_config: ActionConfig,
) -> dict[str, dict[str, Any]]:
    """Load ``instances.json``, tolerating a missing file.

    A missing instances.json means no containers were started
    (for example, when G2P config is generated in a context
    without a running deployment).  Treat it the same as an
    empty instance set: emit outputs and exit cleanly rather
    than failing the action.  Note we only special-case the
    *missing-file* condition here — if the file exists but
    contains invalid JSON, ``load()`` raises ``ConfigError``
    and we deliberately let it propagate, because corrupt
    metadata indicates a broken start-instances run that must
    fail fast rather than silently skip deployment.
    """
    instance_store = InstanceStore(action_config.instances_json_path)
    if not action_config.instances_json_path.exists():
        return {}
    instances: dict[str, dict[str, Any]] = instance_store.load()
    return instances


def _selftest_instance(
    docker: DockerManager,
    cid: str,
    config: G2PConfig,
    slug: str,
) -> G2PSelfTestReport:
    """Run the plumbing self-test for one container and log the outcome.

    Runs immediately after setup so any broken wiring (missing
    hooks.jar, non-executable hook target, empty token, missing
    github-g2p remote, import error in the entry-point script) is
    surfaced now rather than discovered later when a real patchset
    upload silently fails to dispatch a workflow.
    """
    report = selftest_g2p_plumbing(docker, cid, config)
    if report.has_errors:
        failed = [
            c.name for c in report.checks if (not c.passed) and c.severity == "error"
        ]
        logger.error(
            "G2P self-test for instance '%s' failed: %s",
            slug,
            ", ".join(failed),
        )
    else:
        passed = sum(1 for c in report.checks if c.passed)
        total = len(report.checks)
        logger.info(
            "G2P self-test for instance '%s': %d/%d checks passed",
            slug,
            passed,
            total,
        )
    return report


def configure_instances(
    config: G2PConfig,
    docker: DockerManager,
    instances: dict[str, dict[str, Any]],
) -> tuple[list[G2PSetupResult], list[G2PSelfTestReport]]:
    """Deploy G2P into every running container and self-test each one.

    Instances without a recorded container ID are skipped with a
    warning.

    Returns
    -------
    tuple[list[G2PSetupResult], list[G2PSelfTestReport]]
        Per-container setup results and self-test reports, in
        ``instances`` iteration order.
    """
    setup_results: list[G2PSetupResult] = []
    selftest_reports: list[G2PSelfTestReport] = []

    for slug, meta in instances.items():
        cid = meta.get("cid", "")
        if not cid:
            logger.warning("Instance '%s' has no container ID — skipping", slug)
            continue

        with log_group(f"G2P setup: {slug} ({cid[:12]})"):
            result = setup_g2p(config, docker, cid)
            setup_results.append(result)

            logger.info(
                "Instance '%s': config=%s, hooks=%s",
                slug,
                result.config_path,
                result.hooks_enabled,
            )

        with log_group(f"G2P self-test: {slug} ({cid[:12]})"):
            selftest_reports.append(_selftest_instance(docker, cid, config, slug))

    return setup_results, selftest_reports
