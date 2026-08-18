# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Scenario catalogue for the local replication test harness.

Owns the definition of a harness *scenario* — one real-world Gerrit
upstream configuration drawn from the ``GERRIT_SERVERS`` matrix — plus
the curated list of them and the selection/listing helpers the CLI
front-end drives.

Keeping the catalogue here means adding an upstream to exercise is a
data-only change, separate from the container lifecycle and the checks
that run against it.
"""

from __future__ import annotations

import dataclasses
import logging

logger = logging.getLogger(__name__)

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
SCENARIO_MAP: dict[str, Scenario] = {s.name: s for s in SCENARIOS}


def print_scenario_listing() -> None:
    """Print the available scenarios to stdout (``--list`` mode)."""
    print("\nAvailable test scenarios:\n")
    for s in SCENARIOS:
        flag = " [small-disk]" if s.expect_small_disk else ""
        print(f"  {s.name:20s} {s.gerrit_host}{s.api_path}{flag}")
        print(f"  {'':20s} {s.description}")
        print()


def select_scenarios(spec: str | None) -> list[Scenario] | None:
    """Resolve a comma-separated scenario selection.

    Parameters
    ----------
    spec:
        Comma-separated scenario names, or an empty/``None`` value to
        select every scenario.

    Returns
    -------
    list[Scenario] | None
        The selected scenarios in the order requested, or ``None`` if
        any name was unknown (already reported to the log).
    """
    if not spec:
        return list(SCENARIOS)

    names = [n.strip() for n in spec.split(",")]
    selected: list[Scenario] = []
    for name in names:
        if name in SCENARIO_MAP:
            selected.append(SCENARIO_MAP[name])
        else:
            logger.error(
                "Unknown scenario: %r  (available: %s)",
                name,
                ", ".join(SCENARIO_MAP),
            )
            return None
    return selected
