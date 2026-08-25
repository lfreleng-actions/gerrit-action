# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Repository counts, disk usage and completion checks.

Split out of :mod:`replication`; these probes turn the state of a
container's git directory and pull-replication log into the numbers
the wait loop and the verification report are built from.  Every name
here is re-exported from :mod:`replication`.
"""

from __future__ import annotations

import logging

from docker_manager import DockerManager
from errors import DockerError
from replication_model import _parse_int
from replication_patterns import (
    _COMPLETED_COUNT_CMD,
    _COUNT_REPOS_CMD,
    _DISK_USAGE_CMD,
    _DISK_USAGE_HUMAN_CMD,
    _MIN_KB_PER_REPO,
    _REPLICATION_ERROR_PATTERNS,
)
from replication_probe import classify_log_matches

logger = logging.getLogger(__name__)


def get_completed_repo_count(docker: DockerManager, cid: str) -> int:
    """Count UNIQUE repos that have completed replication.

    Parses the pull_replication_log, extracting project names from URLs
    and counting unique entries (excluding system repos).
    """
    try:
        raw = docker.exec_cmd(cid, _COMPLETED_COUNT_CMD, check=False, timeout=15)
        count = _parse_int(raw)
        return count
    except DockerError:
        return 0


def get_log_line_count(docker: DockerManager, cid: str) -> int:
    """Return the number of lines in the pull_replication_log.

    Used by steady-state detection to tell whether new log entries
    are still being written.
    """
    try:
        raw = docker.exec_cmd(
            cid,
            "wc -l < /var/gerrit/logs/pull_replication_log 2>/dev/null || echo 0",
            check=False,
            timeout=10,
        )
        return _parse_int(raw)
    except DockerError:
        return 0


def get_disk_usage_kb(docker: DockerManager, cid: str) -> int:
    """Return the git directory disk usage in kilobytes.

    Unlike :func:`get_git_disk_usage_mb` this returns the raw KB value,
    which is needed for precise change detection in the steady-state
    tracker.
    """
    try:
        raw = docker.exec_cmd(cid, _DISK_USAGE_CMD, check=False, timeout=10)
        return _parse_int(raw)
    except DockerError:
        return 0


def check_pull_replication_log(
    docker: DockerManager,
    cid: str,
    expected_count: int = 0,
    debug: bool = False,
) -> bool:
    """Check if replication has completed successfully.

    Returns *True* ONLY if replication completed without user-project
    errors and the completed count meets the threshold.  Magic-
    repository failures and known-benign soft failures are ignored
    here, exactly as they are by the ``has_user_project_errors`` gate
    the wait loop fails on.
    """
    # Check if log file exists
    if not docker.exec_test(cid, "-f /var/gerrit/logs/pull_replication_log"):
        if debug:
            logger.debug("    pull_replication_log not found")
        return False

    # Check for errors in recent entries.  The grep is only a cheap
    # pre-filter: the matched lines go through the same classifier
    # ``check_replication_errors`` uses, so an expected magic-
    # repository fetch failure or a known-benign soft failure does not
    # block completion.  Treating every recent TransportException as
    # fatal stalled otherwise-healthy runs until timeout, because the
    # repeated ``fetchEvery`` retries of a missing magic ref both kept
    # this check returning False and kept the log line count moving,
    # which in turn denied the steady-state tracker its second
    # completion signal.
    try:
        error_grep = "|".join(_REPLICATION_ERROR_PATTERNS)
        recent_errors = docker.exec_cmd(
            cid,
            f"tail -n 200 /var/gerrit/logs/pull_replication_log 2>/dev/null | "
            f"grep -iE '{error_grep}'",
            check=False,
        )
        matches = classify_log_matches(recent_errors)
        blocking = [m for m in matches if not m.is_magic_repo and not m.is_soft_failure]
        if blocking:
            if debug:
                logger.debug("    Found %d replication error(s) in log", len(blocking))
            return False
        if matches and debug:
            logger.debug(
                "    Ignoring %d expected replication failure(s) in log",
                len(matches),
            )
    except DockerError as exc:
        logger.debug("Could not tail pull_replication_log: %s", exc)

    completed_count = get_completed_repo_count(docker, cid)
    if debug:
        logger.debug(
            "    check_pull_replication_log: completed=%d, expected=%d",
            completed_count,
            expected_count,
        )

    if expected_count > 0:
        # Require at least 90% of expected repos
        min_completed = expected_count * 9 // 10
        if debug:
            logger.debug("    min_completed (90%%)=%d", min_completed)
        if completed_count >= min_completed:
            if debug:
                logger.debug("    Success: %d >= %d", completed_count, min_completed)
            return True
        if debug:
            logger.debug("    Not enough: %d < %d", completed_count, min_completed)
        return False
    else:
        # No expected count — any completion is good
        if completed_count > 0:
            if debug:
                logger.debug(
                    "    No expected count, found %d completions", completed_count
                )
            return True

    return False


# ---------------------------------------------------------------------------
# Repository counting and disk usage
# ---------------------------------------------------------------------------


def count_repositories(docker: DockerManager, cid: str) -> int:
    """Count replicated repositories (excludes All-Projects and All-Users).

    Uses ``-prune`` to avoid descending into .git directories and
    verifies each directory is a bare repo by checking for HEAD file.
    """
    try:
        raw = docker.exec_cmd(cid, _COUNT_REPOS_CMD, check=False, timeout=15)
        return _parse_int(raw)
    except DockerError:
        return 0


def get_git_disk_usage_mb(docker: DockerManager, cid: str) -> int:
    """Return the git directory disk usage in megabytes."""
    try:
        raw = docker.exec_cmd(cid, _DISK_USAGE_CMD, check=False, timeout=10)
        size_kb = _parse_int(raw)
        return size_kb // 1024
    except DockerError:
        return 0


def get_git_disk_usage_human(docker: DockerManager, cid: str) -> str:
    """Return human-readable git directory disk usage."""
    try:
        result: str = docker.exec_cmd(
            cid, _DISK_USAGE_HUMAN_CMD, check=False, timeout=10
        )
        return result
    except DockerError:
        return "?"


def check_replication_has_content(
    docker: DockerManager,
    cid: str,
    expected_count: int = 0,
    min_size_mb: int = 0,
) -> bool:
    """Check if replication has fetched substantial content.

    An empty bare git repo created by ``createMissingRepositories`` is
    roughly 150 KB.  We consider a repository to have *real* content
    when its average size exceeds :data:`_MIN_KB_PER_REPO` (200 KB).

    The previous hard-coded 100 MB floor caused false negatives for
    collections of small repositories (e.g. 36 ansible-role repos
    totalling 86 MB — well above empty, but below the old threshold).

    Parameters
    ----------
    docker, cid:
        Docker manager / container ID.
    expected_count:
        Number of repositories expected.  When > 0 we estimate a
        per-repo minimum; otherwise we fall back to *min_size_mb*.
    min_size_mb:
        Absolute minimum MB.  Defaults to 0 so the per-repo heuristic
        is the primary check.  Callers may pass a value for cases
        where no expected count is available.
    """
    current_kb = get_disk_usage_kb(docker, cid)
    current_mb = current_kb // 1024

    if expected_count > 0:
        # Scale the threshold to the actual number of repos.
        # _MIN_KB_PER_REPO (200 KB) is ~33% above the size of an
        # empty bare repo, so exceeding this means real objects exist.
        estimated_min_kb = expected_count * _MIN_KB_PER_REPO
        estimated_min_mb = max(estimated_min_kb // 1024, 1)
        threshold_mb = max(estimated_min_mb, min_size_mb)
    else:
        # No expected count — use 1 MB as a sanity floor.
        threshold_mb = max(1, min_size_mb)

    return current_mb >= threshold_mb


def list_repositories(
    docker: DockerManager,
    cid: str,
    max_items: int = 20,
) -> str:
    """List repositories in the git directory.

    Returns a newline-separated string of repository paths.
    """
    try:
        result: str = docker.exec_cmd(
            cid,
            f"find /var/gerrit/git -name '*.git' -type d -prune 2>/dev/null | "
            f"head -{max_items}",
            check=False,
            timeout=15,
        )
        return result
    except DockerError:
        return "(none found)"


def show_pull_replication_log(
    docker: DockerManager,
    cid: str,
    lines: int = 50,
) -> str:
    """Return the last N lines of the pull_replication_log."""
    if not docker.exec_test(cid, "-f /var/gerrit/logs/pull_replication_log"):
        if docker.exec_test(cid, "-e /var/gerrit/logs/pull_replication_log"):
            return "(empty)"
        return "(file not found)"

    try:
        content = docker.exec_cmd(
            cid,
            f"tail -n {lines} /var/gerrit/logs/pull_replication_log 2>/dev/null",
            check=False,
            timeout=10,
        )
        return content if content.strip() else "(empty)"
    except DockerError:
        return "(error reading log)"
