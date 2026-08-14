# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The job summary reported once every instance has been started.

:func:`write_startup_summary` renders the markdown table that GitHub
Actions shows on the job page, listing each started instance with the
host ports it was mapped to.  It is the only place the start-up phase
writes to the step summary, so the table's shape lives here rather than
in the orchestrator.
"""

from __future__ import annotations

from config import InstanceStore
from outputs import write_summary


def write_startup_summary(instance_store: InstanceStore) -> None:
    """Write the step summary table for started instances."""
    lines = [
        "**Instances Started** 🚀",
        "",
        "| Slug | HTTP Port | SSH Port | Status |",
        "|------|-----------|----------|--------|",
    ]
    for slug, meta in instance_store:
        http_port = meta.get("http_port", "?")
        ssh_port = meta.get("ssh_port", "?")
        lines.append(f"| {slug} | {http_port} | {ssh_port} | ✅ Running |")
    lines.append("")
    write_summary("\n".join(lines))
