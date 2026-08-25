# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Scenario catalogue and result records for the replication harness.

Split out of ``scripts/test-replication-local.py`` so the container,
check, tunnel, report and CLI modules can share the scenario
definitions, the per-test and per-scenario result records and the
option bundles without importing each other.  Every name here is
re-exported from the harness entry point.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

# The harness lives in ``scripts/``; this module lives one level deeper
# in ``scripts/lib``.  Resolving upwards keeps the sibling-script paths
# (``Dockerfile``, ``verify-tunnel.py``) identical to the entry point's
# own ``SCRIPT_DIR``.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

# Each scenario represents a real-world Gerrit project configuration
# from the GERRIT_SERVERS matrix.  ``expected_project_count`` is the
# approximate number of repositories the upstream has — the test will
# validate that the detection logic works correctly for this size.


@dataclasses.dataclass
class Scenario:
    """A single test scenario exercising a specific upstream configuration."""

    name: str
    description: str
    gerrit_host: str
    api_path: str
    project_filter: str = ""
    expected_project_count: int = 0
    # When True, the scenario is expected to have small total disk (the
    # bug-report case).  The test will *assert* that the old 100 MB
    # threshold would have been wrong.
    expect_small_disk: bool = False
    slug: str = ""

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = self.name


# Curated set of scenarios covering different repository sizes and counts.
# The list is ordered from smallest (most likely to trigger the old bug)
# to largest.
SCENARIOS: list[Scenario] = [
    Scenario(
        name="lf-small",
        description=(
            "Linux Foundation infra — many small repos (ansible roles, "
            "puppet modules).  This is the exact configuration that "
            "triggered the 600-second timeout bug: 36 repos at 86 MB."
        ),
        gerrit_host="gerrit.linuxfoundation.org",
        api_path="/infra",
        expected_project_count=36,
        expect_small_disk=True,
        slug="lf",
    ),
    Scenario(
        name="lf-single",
        description=(
            "Linux Foundation infra — single project filter. "
            "Tests that project-filtered replication detects "
            "completion for a single repo without false negatives."
        ),
        gerrit_host="gerrit.linuxfoundation.org",
        api_path="/infra",
        project_filter="releng/lftools",
        expected_project_count=1,
        expect_small_disk=True,
        slug="lf-single",
    ),
    Scenario(
        name="onap",
        description=(
            "ONAP — medium-sized project set.  Tests the standard "
            "case where the old threshold would have (eventually) passed."
        ),
        gerrit_host="gerrit.onap.org",
        api_path="/r",
        expected_project_count=10,
        slug="onap",
    ),
    Scenario(
        name="opnfv",
        description=(
            "OPNFV — small set of infrastructure repos.  Another "
            "case that may fall below the old 100 MB floor."
        ),
        gerrit_host="gerrit.opnfv.org",
        api_path="/gerrit",
        expected_project_count=5,
        expect_small_disk=True,
        slug="opnfv",
    ),
    Scenario(
        name="o-ran-sc",
        description=(
            "O-RAN Software Community — medium project set.  "
            "Tests pull-replication with a different Gerrit host."
        ),
        gerrit_host="gerrit.o-ran-sc.org",
        api_path="/r",
        expected_project_count=10,
        slug="o-ran-sc",
    ),
]

# Quick lookup by name
_SCENARIO_MAP: dict[str, Scenario] = {s.name: s for s in SCENARIOS}


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------

# Base port incremented per scenario to avoid conflicts.
_BASE_HTTP_PORT = 18080
_BASE_SSH_PORT = 39418


@dataclasses.dataclass
class _ContainerContext:
    """Tracks a running Gerrit container for a single scenario."""

    cid: str
    name: str
    http_port: int
    ssh_port: int
    work_dir: Path


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TestResult:
    """Outcome of a single test within a scenario."""

    name: str
    passed: bool
    message: str = ""
    elapsed_s: float = 0.0

    def __str__(self) -> str:
        icon = "✅" if self.passed else "❌"
        msg = f" — {self.message}" if self.message else ""
        timing = f" ({self.elapsed_s:.1f}s)" if self.elapsed_s else ""
        return f"  {icon} {self.name}{msg}{timing}"


@dataclasses.dataclass
class ScenarioResult:
    """Aggregated result for one scenario."""

    scenario: Scenario
    tests: list[TestResult] = dataclasses.field(default_factory=list)
    container_started: bool = False
    gerrit_ready: bool = False
    error: str = ""

    @property
    def passed(self) -> bool:
        """True when the container came up and every test passed.

        ``error`` is consulted as well as the test outcomes, because a
        scenario can fail *after* Gerrit reports ready — an exception
        from the initial-cycle wait or from a check part-way through
        leaves the earlier results in place and records the reason
        here.  Without this the run would keep its green icon and exit
        zero on the strength of the checks that happened to complete
        before the failure.
        """
        if not self.container_started or not self.gerrit_ready:
            return False
        if self.error:
            return False
        return all(t.passed for t in self.tests)

    @property
    def total_elapsed(self) -> float:
        """Wall-clock seconds accumulated by this scenario's tests."""
        return sum(t.elapsed_s for t in self.tests)


# ---------------------------------------------------------------------------
# Option bundles
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScenarioRunOptions:
    """Run-level inputs every scenario in a single harness run shares.

    Groups the resolved image and credentials with the timing knobs so
    :func:`run_scenario` takes one options record instead of a long
    keyword-only tail.
    """

    image: str
    creds: tuple[str, str]
    timeout: int
    stability_window: int
    fetch_every: str
    keep: bool = False


@dataclasses.dataclass(frozen=True)
class HarnessConfig:
    """Environment-derived tuning for one harness invocation."""

    gerrit_version: str
    plugin_version: str
    timeout: int
    stability_window: int
    fetch_every: str
