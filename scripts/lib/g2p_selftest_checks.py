# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Individual probes that validate the in-container G2P plumbing.

Each probe interrogates one aspect of a configured Gerrit container
(plugin presence, hook wiring, INI contents, replication remote,
entry-point importability) and returns one or more
:class:`G2PSelfTestCheck` records.  Probes never raise on diagnostic
commands — a failed command is itself a finding — so the caller can
run the whole suite and report every problem at once.

Usage::

    from g2p_selftest_checks import check_hooks_plugin

    report.checks.append(check_hooks_plugin(docker, cid))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from docker_manager import DockerManager
from g2p_paths import (
    G2P_INI_PATH,
    GERRIT_HOOKS_DIR,
    GERRIT_PLUGINS_DIR,
    GERRIT_REPLICATION_CONFIG,
    GERRIT_TOOLS_VENV_BIN,
)

logger = logging.getLogger(__name__)


@dataclass
class G2PSelfTestCheck:
    """Single self-test outcome.

    Attributes:
        name: Short machine-readable identifier.
        passed: ``True`` when the check succeeded.
        severity: ``"error"`` (G2P will not work), ``"warning"``
            (likely degraded), or ``"info"`` (advisory).
        message: Human-readable detail string.
    """

    name: str
    passed: bool
    severity: str = "error"
    message: str = ""


# ---------------------------------------------------------------------------
# Probe primitives
# ---------------------------------------------------------------------------


def _selftest_check(
    name: str,
    *,
    passed: bool,
    severity: str = "error",
    message: str = "",
) -> G2PSelfTestCheck:
    """Build a :class:`G2PSelfTestCheck` and emit a matching log line.

    Centralised so each check is reported once at the appropriate
    log level (info on pass, warning/error on fail) without each
    call site having to repeat the formatting.
    """
    check = G2PSelfTestCheck(
        name=name,
        passed=passed,
        severity=severity,
        message=message,
    )
    if passed:
        logger.info("Self-test ✅ %s — %s", name, message or "ok")
    elif severity == "warning":
        logger.warning("Self-test ⚠️  %s — %s", name, message)
    else:
        logger.error("Self-test ❌ %s — %s", name, message)
    return check


def _exec_or_blank(
    docker: DockerManager,
    cid: str,
    command: str,
    *,
    user: str | None = None,
) -> str:
    """Run ``command`` inside ``cid``; return stdout or '' on failure.

    The self-test helpers should never raise on diagnostic commands;
    they should only record the failure.  This wrapper hides the
    ``check=False`` plumbing.
    """
    try:
        out: str = docker.exec_cmd(cid, command, user=user, check=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Self-test exec failed for %r: %s", command, exc)
        return ""
    return out.strip()


# ---------------------------------------------------------------------------
# Plugin probes
# ---------------------------------------------------------------------------


def check_hooks_plugin(docker: DockerManager, cid: str) -> G2PSelfTestCheck:
    """Assert ``hooks.jar`` is present in ``/var/gerrit/plugins/``."""
    hooks_jar = f"{GERRIT_PLUGINS_DIR}/hooks.jar"
    has_hooks_jar = docker.exec_test(cid, f"-f {hooks_jar}")
    return _selftest_check(
        "hooks_plugin_present",
        passed=has_hooks_jar,
        severity="error",
        message=(
            f"{hooks_jar} present"
            if has_hooks_jar
            else (
                f"{hooks_jar} missing — Gerrit will not invoke "
                "any hook scripts under /var/gerrit/hooks/"
            )
        ),
    )


def check_bundled_replication_removed(
    docker: DockerManager,
    cid: str,
) -> G2PSelfTestCheck:
    """Assert the bundled ``replication.jar`` was removed.

    We replace it with pull-replication; if it is still there the
    bundled init grabbed a copy and it must be removed.
    """
    bundled_repl = f"{GERRIT_PLUGINS_DIR}/replication.jar"
    bundled_present = docker.exec_test(cid, f"-f {bundled_repl}")
    return _selftest_check(
        "bundled_replication_removed",
        passed=not bundled_present,
        severity="warning",
        message=(
            "bundled replication.jar removed (pull-replication takes over)"
            if not bundled_present
            else (f"{bundled_repl} still present — may conflict with pull-replication")
        ),
    )


# ---------------------------------------------------------------------------
# Hook wiring probes
# ---------------------------------------------------------------------------


def _check_hook_wrapper(
    docker: DockerManager,
    cid: str,
    hook_name: str,
) -> list[G2PSelfTestCheck]:
    """Validate one installed hook wrapper and its console script.

    The check name (``hook_symlink_*``) is kept stable for any
    downstream consumers that filter on it, even though the install
    layer has moved from a bare ``ln -sf`` symlink to a POSIX-shell
    wrapper.  We deliberately do NOT use ``readlink -f`` here: on a
    regular wrapper file ``readlink -f`` resolves to the wrapper
    itself, which would validate nothing about the underlying
    console script the wrapper exec()s.  Instead we parse the
    wrapper's ``TARGET=`` line so the check actually asserts what it
    claims to.
    """
    hook_path = f"{GERRIT_HOOKS_DIR}/{hook_name}"

    # The wrapper itself must exist and be executable.
    if not docker.exec_test(cid, f"-f {hook_path}"):
        return [
            _selftest_check(
                f"hook_symlink_{hook_name}",
                passed=False,
                severity="error",
                message=f"{hook_path} missing",
            )
        ]

    # Extract the TARGET shell variable from the wrapper body.
    # The wrapper installs a single ``TARGET='...'`` line; we
    # grep + sed it out rather than executing the wrapper.
    target = _exec_or_blank(
        docker,
        cid,
        (
            f'grep -m1 "^TARGET=" {hook_path} | '
            'sed -E "s/^TARGET=[\'\\"]?//; s/[\'\\"]?$//"'
        ),
    )
    if not target:
        return [
            _selftest_check(
                f"hook_symlink_{hook_name}",
                passed=False,
                severity="error",
                message=(f"{hook_path} has no TARGET= line; wrapper looks malformed"),
            )
        ]

    checks = [
        _selftest_check(
            f"hook_symlink_{hook_name}",
            passed=True,
            severity="info",
            message=f"{hook_path} -> {target}",
        )
    ]

    # The underlying console script must be present and runnable
    # as the gerrit user (UID 1000, the runtime user Gerrit uses
    # to fork hook processes).
    is_exec = docker.exec_test(cid, f"-x {target}") and docker.exec_test(
        cid, f"-r {target}"
    )
    checks.append(
        _selftest_check(
            f"hook_target_executable_{hook_name}",
            passed=is_exec,
            severity="error",
            message=(
                f"{target} executable+readable"
                if is_exec
                else (
                    f"{target} not executable+readable; "
                    "Gerrit will skip the hook silently"
                )
            ),
        )
    )
    return checks


def check_hook_wrappers(
    docker: DockerManager,
    cid: str,
    hooks: list[str],
) -> list[G2PSelfTestCheck]:
    """Validate every enabled hook wrapper, in ``hooks`` order."""
    checks: list[G2PSelfTestCheck] = []
    for hook_name in hooks:
        checks.extend(_check_hook_wrapper(docker, cid, hook_name))
    return checks


def check_hook_entrypoint(
    docker: DockerManager,
    cid: str,
    hooks: list[str],
) -> list[G2PSelfTestCheck]:
    """Prove the first hook's console script imports cleanly.

    Returns an empty list when no hooks are enabled.
    """
    if not hooks:
        return []

    first_hook = hooks[0]
    first_target = f"{GERRIT_TOOLS_VENV_BIN}/{first_hook}"
    # ``--help`` exits 0 and prints usage on the python entry
    # point without dispatching anything.  We tolerate any
    # 0/1/2 exit code; what we really want is "no traceback".
    help_output = _exec_or_blank(
        docker,
        cid,
        f"{first_target} --help 2>&1 || true",
        user="gerrit",
    )
    traceback_seen = "Traceback" in help_output
    return [
        _selftest_check(
            "hook_entrypoint_imports",
            passed=not traceback_seen,
            severity="error",
            message=(
                f"{first_target} --help ran cleanly as gerrit"
                if not traceback_seen
                else (
                    f"{first_target} --help raised a Python "
                    f"traceback when run as gerrit:\n{help_output[:400]}"
                )
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Configuration probes
# ---------------------------------------------------------------------------


def check_g2p_ini(docker: DockerManager, cid: str) -> list[G2PSelfTestCheck]:
    """Assert the INI exists, is non-empty, and carries a token."""
    ini_present = docker.exec_test(cid, f"-s {G2P_INI_PATH}")
    if not ini_present:
        return [
            _selftest_check(
                "g2p_ini_present",
                passed=False,
                severity="error",
                message=f"{G2P_INI_PATH} missing or empty",
            )
        ]

    checks = [
        _selftest_check(
            "g2p_ini_present",
            passed=True,
            severity="info",
            message=f"{G2P_INI_PATH} present and non-empty",
        )
    ]
    token_line = _exec_or_blank(
        docker,
        cid,
        f"grep -E '^[[:space:]]*token[[:space:]]*=' {G2P_INI_PATH} || true",
    )
    # A bare ``token =`` (or absent line) means dispatch will
    # fail at runtime; flag clearly.
    token_value = ""
    if token_line and "=" in token_line:
        token_value = token_line.split("=", 1)[1].strip()
    token_ok = bool(token_value)
    checks.append(
        _selftest_check(
            "g2p_ini_token_populated",
            passed=token_ok,
            severity="error",
            message=(
                "INI carries a non-empty token"
                if token_ok
                else (
                    f"INI {G2P_INI_PATH} has no populated token "
                    "line — workflow_dispatch will fail at runtime"
                )
            ),
        )
    )
    return checks


def check_replication_remote(docker: DockerManager, cid: str) -> G2PSelfTestCheck:
    """Assert ``replication.config`` carries a github-detection remote.

    The ``authGroup`` value must contain the substring ``github``
    (case-insensitive) — that is the platform-detection gate
    ``gerrit_to_platform`` uses.
    """
    repl_authgroup = _exec_or_blank(
        docker,
        cid,
        # Print just the authGroup value(s) under the github-g2p
        # section.  awk handles the section scoping safely.
        (
            "awk '"
            '/^\\[remote "github-g2p"\\]/ {in_sec=1; next} '
            "/^\\[/ {in_sec=0} "
            "in_sec && /authGroup[[:space:]]*=/ {print}'"
            f" {GERRIT_REPLICATION_CONFIG}"
        ),
    )
    has_github_authgroup = bool(repl_authgroup) and "github" in repl_authgroup.lower()
    return _selftest_check(
        "replication_github_remote",
        passed=has_github_authgroup,
        severity="error",
        message=(
            f"github-g2p remote present (authGroup: {repl_authgroup})"
            if has_github_authgroup
            else (
                f"github-g2p remote missing or its authGroup does "
                f"not contain 'github' in {GERRIT_REPLICATION_CONFIG} "
                "— platform detection will fail and no dispatch "
                "will fire"
            )
        ),
    )
