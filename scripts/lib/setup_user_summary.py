# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Console and GitHub Actions step summary reporting.

Split out of ``setup-gerrit-user.py``: this module owns everything
written for human consumption once the accounts have been configured,
both the closing log block and the ``$GITHUB_STEP_SUMMARY`` sections.
Every name here is re-exported from ``setup-gerrit-user.py``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def output_github_summary(account: dict, username: str) -> None:
    """Write summary to GitHub Actions step summary if available."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    try:
        with open(summary_file, "a") as f:
            f.write("### Gerrit User Setup ✅\n\n")
            f.write(f"**Username:** `{username}`\n")
            f.write(f"**Account ID:** `{account.get('_account_id', 'unknown')}`\n")
            if account.get("email"):
                f.write(f"**Email:** `{account['email']}`\n")
            f.write("\n**Permissions:** Administrator (full create/merge access)\n")
    except OSError as e:
        logger.warning(f"Failed to write GitHub summary: {e}")


def output_multi_instance_summary(
    username: str,
    rows: list[tuple[str, str]],
    failure_count: int,
) -> None:
    """Write a multi-instance summary to GitHub Actions step summary."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    try:
        with open(summary_file, "a") as f:
            if failure_count == 0:
                f.write("### SSH Access Configured 🔑\n")
            else:
                f.write("### SSH Access Configuration ⚠️\n")
            f.write("\n")
            f.write(
                "SSH public keys have been processed for the Gerrit container(s).\n"
            )
            f.write("\n")
            f.write("| Instance | Status |\n")
            f.write("|----------|--------|\n")
            for slug, status in rows:
                f.write(f"| {slug} | {status} |\n")
            f.write("\n")
            f.write(f"**Username:** `{username}`\n")
            if username != "admin":
                f.write("\n")
                f.write("**Permissions:** Administrator (full create/merge access)\n")
            f.write("\n")
            f.write("**SSH Command:**\n")
            f.write("```bash\n")
            f.write(f"ssh -p 29418 {username}@<gerrit-host>\n")
            f.write("```\n")
    except OSError as e:
        logger.warning(f"Failed to write GitHub summary: {e}")


def log_loop_completion(
    username: str,
    success_count: int,
    failure_count: int,
) -> None:
    """Log the closing banner for a multi-instance run."""
    logger.info("")
    logger.info("========================================")
    if failure_count == 0:
        logger.info("SSH authentication keys configured ✅")
    else:
        logger.info(f"SSH authentication completed with {failure_count} failure(s)")
    logger.info("========================================")
    logger.info("")
    logger.info(f"Configured {success_count} instance(s) successfully")
    logger.info(f"You can now SSH to the Gerrit container(s) as '{username}'")
    logger.info(f"Example: ssh -p 29418 {username}@<host>")

    if username != "admin":
        logger.info("")
        logger.info(f"User '{username}' has been added to the Administrators group.")
        logger.info("This grants full permissions to create and merge changes.")
