# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Aggregation of the G2P plumbing self-test into a single report.

This module owns the *order* in which the individual probes from
:mod:`g2p_selftest_checks` run and the aggregate view
(:class:`G2PSelfTestReport`) that ``configure-g2p.py`` uses to decide
whether the deployment step should go red.  Keeping the ordering here
means a new probe is a one-line addition next to its peers.

Usage::

    from g2p_selftest import selftest_g2p_plumbing

    report = selftest_g2p_plumbing(docker, cid, config)
    if report.has_errors:
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field

from docker_manager import DockerManager
from g2p_config import G2PConfig
from g2p_selftest_checks import (
    G2PSelfTestCheck,
    check_bundled_replication_removed,
    check_g2p_ini,
    check_hook_entrypoint,
    check_hook_wrappers,
    check_hooks_plugin,
    check_replication_remote,
)

__all__ = [
    "G2PSelfTestCheck",
    "G2PSelfTestReport",
    "selftest_g2p_plumbing",
]


@dataclass
class G2PSelfTestReport:
    """Aggregated self-test outcomes for a container.

    Attributes:
        cid: Container identifier the self-test ran against.
        checks: Ordered list of individual check outcomes.
    """

    cid: str = ""
    checks: list[G2PSelfTestCheck] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        """Return True when every check passed."""
        return all(c.passed for c in self.checks)

    @property
    def has_errors(self) -> bool:
        """Return True when any error-severity check failed."""
        return any((not c.passed) and c.severity == "error" for c in self.checks)


def selftest_g2p_plumbing(
    docker: DockerManager,
    cid: str,
    config: G2PConfig,
) -> G2PSelfTestReport:
    """Validate the in-container G2P plumbing end-to-end.

    Runs a sequence of independent checks against the running
    Gerrit container and collects the results into a
    :class:`G2PSelfTestReport`.  The checks are deliberately
    side-effect free apart from a final synthetic hook invocation
    that targets a non-existent change so it cannot mutate any
    real GitHub state.

    Steps:

    1. ``hooks.jar`` is present in ``/var/gerrit/plugins/``.
    2. ``replication.jar`` is **absent** (we replace it with
       pull-replication; if it's there the bundled init grabbed
       a copy and it must be removed).
    3. Each enabled hook wrapper in ``/var/gerrit/hooks/`` exists
       and points at an executable console-script target.
    4. Each hook target is executable as the ``gerrit`` user.
    5. ``gerrit_to_platform.ini`` exists, is non-empty, and contains
       a ``token = `` line whose value is non-empty.
    6. ``replication.config`` contains a ``[remote "github-g2p"]``
       section whose ``authGroup`` value contains the substring
       ``github`` (case-insensitive) — the platform-detection
       gate ``gerrit_to_platform`` uses.
    7. The ``gerrit`` user can ``--help`` the patchset-created
       script (proves the venv shebang resolves and the entry
       point imports cleanly).

    Parameters
    ----------
    docker:
        :class:`DockerManager` instance.
    cid:
        Running container id or name.
    config:
        Validated :class:`G2PConfig` (used to know which hooks
        should have been enabled).

    Returns
    -------
    G2PSelfTestReport
        Aggregated outcomes, in the order listed above.
    """
    report = G2PSelfTestReport(cid=cid)

    # 1. hooks.jar present
    report.checks.append(check_hooks_plugin(docker, cid))

    # 2. bundled replication.jar removed
    report.checks.append(check_bundled_replication_removed(docker, cid))

    # 3 & 4. Hook wrappers exist and their underlying TARGET
    # console script is executable.
    report.checks.extend(check_hook_wrappers(docker, cid, config.hooks))

    # 5. INI exists and has a non-empty token
    report.checks.extend(check_g2p_ini(docker, cid))

    # 6. replication.config carries a github-detection remote
    report.checks.append(check_replication_remote(docker, cid))

    # 7. Hook script imports cleanly when run as gerrit
    report.checks.extend(check_hook_entrypoint(docker, cid, config.hooks))

    return report
