# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Rendering of g2p check results for the three consumers.

Check results leave the audit as plain
:class:`~g2p_github_model.G2PCheckResult` records; this module turns
them into the shapes the surrounding GitHub Actions run expects:

* :func:`format_check_results` — workflow log annotations, plus the
  fatal/non-fatal verdict derived from the validation mode.
* :func:`results_to_json` — the machine-readable action output.
* :func:`format_check_results_summary` — the Markdown table written
  to ``$GITHUB_STEP_SUMMARY``.

Nothing here talks to GitHub; it is pure presentation.
"""

from __future__ import annotations

import json
import logging

from g2p_github_model import G2PCheckResult

logger = logging.getLogger(__name__)


def format_check_results(
    results: list[G2PCheckResult],
    mode: str,
) -> tuple[list[str], bool]:
    """Format check results as GitHub Actions annotations.

    Parameters
    ----------
    results:
        Check results from :func:`check_github_config`.
    mode:
        Validation mode (``"error"``, ``"warn"``, or ``"skip"``).

    Returns
    -------
    tuple[list[str], bool]
        A list of annotation strings and a boolean indicating whether
        any fatal failures occurred (only *True* when *mode* is
        ``"error"`` and a check with ``severity="error"`` failed).
    """
    annotations: list[str] = []
    has_fatal = False

    for result in results:
        if result.passed:
            logger.info("%s", result)
            continue

        if result.severity == "error":
            if mode == "error":
                # Strict mode: annotate once via the logger
                # (the _GitHubActionsFormatter emits ::error::) and
                # record the annotation string for callers that need
                # to surface it elsewhere (e.g. test assertions or a
                # collated summary). Marks the run fatal.
                logger.error("%s", result.message)
                annotations.append(f"::error::{result.message}")
                has_fatal = True
            elif mode == "warn":
                logger.warning("%s", result.message)
                annotations.append(f"::warning::{result.message}")
            # mode == "skip" should never reach here
        elif result.severity == "warning":
            logger.warning("%s", result.message)
            annotations.append(f"::warning::{result.message}")
        else:
            logger.info("%s", result)

    return annotations, has_fatal


def results_to_json(results: list[G2PCheckResult]) -> str:
    """Serialise check results to a JSON string for action outputs.

    Parameters
    ----------
    results:
        Check results.

    Returns
    -------
    str
        JSON array of check result objects.
    """
    return json.dumps(
        [
            {
                "check_name": r.check_name,
                "passed": r.passed,
                "message": r.message,
                "severity": r.severity,
            }
            for r in results
        ],
        indent=2,
    )


def format_check_results_summary(
    results: list[G2PCheckResult],
    owner: str,
    mode: str,
    provisioned: list[str] | None = None,
) -> str:
    """Render check results as a Markdown summary table.

    Parameters
    ----------
    results:
        Check outcomes from the audit phase.
    owner:
        GitHub org name (for the heading).
    mode:
        The ``g2p_org_setup`` mode value.
    provisioned:
        Descriptions of items auto-provisioned
        (used when mode is ``'provision'``).

    Returns
    -------
    str
        Markdown content for ``$GITHUB_STEP_SUMMARY``.
    """
    lines: list[str] = [
        f"## G2P Organisation Audit: `{owner}`",
        "",
        "| Check | Status | Details |",
        "|-------|--------|---------|",
    ]

    def _md_table_cell(text: str) -> str:
        """Escape characters that break Markdown table cells."""
        return text.replace("|", r"\|").replace("\n", "<br>")

    for r in results:
        status = "PASS ✅" if r.passed else "FAIL ❌"
        if not r.passed and r.severity == "warning":
            status = "WARN ⚠️"
        name = _md_table_cell(r.check_name)
        msg = _md_table_cell(r.message)
        lines.append(f"| {name} | {status} | {msg} |")

    lines.append("")

    if mode == "provision":
        lines.append("**Mode:** `provision` — auto-provisioning enabled.")
    elif mode == "verify":
        lines.append("**Mode:** `verify` — reporting only, no changes made.")
    else:
        lines.append(f"**Mode:** `{mode}`")

    lines.append("")

    if provisioned:
        lines.append("### Provisioned Items")
        lines.append("")
        for item in provisioned:
            lines.append(f"- {item}")
        lines.append("")

    # List absent items when in verify mode
    if mode == "verify":
        absent: list[str] = []
        for r in results:
            if not r.passed and r.severity == "error":
                absent.append(f"- **{r.check_name}**: {r.message}")
        if absent:
            lines.append("### Absent Items")
            lines.append("")
            lines.extend(absent)
            lines.append("")

    return "\n".join(lines)
