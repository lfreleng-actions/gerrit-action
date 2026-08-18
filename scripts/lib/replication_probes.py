# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read-only probes of a Gerrit container's replication state.

Every function here answers a single question about a running
container — "does ``replication.config`` exist?", "how many bare
repos are on disk?", "how much has the git tree grown?" — by running
a short shell command inside it.  They are deliberately side-effect
free and forgiving: a :class:`~errors.DockerError` yields a neutral
value (``0``, ``False``, ``"?"``) rather than propagating, because
the callers poll these in a loop where a single failed ``docker
exec`` must not abort the wait.

The shell commands themselves are module constants so their (fairly
subtle) ``sed``/``find`` pipelines are documented in one place.
"""

from __future__ import annotations

import logging
import re

from docker_manager import DockerManager
from errors import DockerError
from replication_scan import _REPLICATION_ERROR_PATTERNS

logger = logging.getLogger(__name__)

# Minimum per-repo disk size in KB that indicates real content.
# An empty bare git repo is ~150 KB; anything above ~200 KB/repo
# means actual refs/objects were fetched.
_MIN_KB_PER_REPO = 200

# Pattern to extract unique completed repo names from pull_replication_log
# Log format: "[timestamp] [id] Replication from <url> completed in ..."
# URL formats:
#   - HTTPS: https://gerrit.example.org/r/a/<project>.git
#   - SSH:   ssh://gerrit.example.org:29418/<project>.git
#
# Extraction: strip prefix through "Replication from ", strip ".git completed..."
# suffix, strip /a/ path for HTTP, strip scheme://authority/ for SSH.
_COMPLETED_COUNT_CMD = (
    "grep 'Replication from .* completed' "
    "/var/gerrit/logs/pull_replication_log 2>/dev/null | "
    "sed -E '"
    "s|.*Replication from ||; "
    "s|\\.git completed.*||; "
    "s|.*/a/||; "
    "s|^[^:]+://[^/]+/||"
    "' | "
    "grep -v -E '^All-Projects$|^All-Users$' | "
    "sort -u | wc -l"
)

# Command to count bare git repos excluding system repos
_COUNT_REPOS_CMD = (
    "find /var/gerrit/git -name '*.git' -type d -prune 2>/dev/null | "
    "while read -r dir; do "
    '  if [ -f "$dir/HEAD" ]; then echo "$dir"; fi; '
    "done | "
    "grep -v -E 'All-Projects|All-Users' | wc -l"
)

# Command to get git directory disk usage in KB
_DISK_USAGE_CMD = "du -sk /var/gerrit/git 2>/dev/null | cut -f1"

# Command to get human-readable disk usage
_DISK_USAGE_HUMAN_CMD = "du -sh /var/gerrit/git 2>/dev/null | cut -f1"


def _parse_int(raw: str) -> int:
    """Parse a string to int, stripping non-digit characters.

    Returns 0 if the string contains no digits.
    """
    digits = re.sub(r"[^0-9]", "", raw.strip())
    return int(digits) if digits else 0


# ---------------------------------------------------------------------------
# Plugin and configuration checks
# ---------------------------------------------------------------------------


def check_replication_config(docker: DockerManager, cid: str) -> bool:
    """Verify that ``replication.config`` exists in the container.

    Returns *True* if the file exists.
    """
    result: bool = docker.exec_test(cid, "-f /var/gerrit/etc/replication.config")
    return result


def show_replication_config(docker: DockerManager, cid: str) -> str:
    """Read and return the replication config (excluding comments/blanks).

    Returns the config content or an empty string.
    """
    try:
        raw = docker.exec_cmd(
            cid,
            "cat /var/gerrit/etc/replication.config 2>/dev/null",
            check=False,
        )
        # Filter out comments and blank lines
        lines = [
            line
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return "\n".join(lines)
    except DockerError:
        return ""


def check_secure_config(docker: DockerManager, cid: str) -> bool:
    """Check if secure.config exists and log its sections.

    Returns *True* if the file exists.
    """
    if not docker.exec_test(cid, "-f /var/gerrit/etc/secure.config"):
        logger.warning("secure.config not found")
        return False

    logger.info("  secure.config exists ✅")
    try:
        sections = docker.exec_cmd(
            cid,
            "grep '^\\[' /var/gerrit/etc/secure.config 2>/dev/null",
            check=False,
        )
        if sections:
            logger.info("  secure.config sections:")
            for line in sections.splitlines():
                logger.info("    %s", line)
    except DockerError as exc:
        logger.debug("Could not read secure.config sections: %s", exc)
    return True


# ---------------------------------------------------------------------------
# Replication log counters
# ---------------------------------------------------------------------------


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

    Returns *True* ONLY if replication completed WITHOUT errors and
    the completed count meets the threshold.
    """
    # Check if log file exists
    if not docker.exec_test(cid, "-f /var/gerrit/logs/pull_replication_log"):
        if debug:
            logger.debug("    pull_replication_log not found")
        return False

    # Check for errors in recent entries
    try:
        error_grep = "|".join(_REPLICATION_ERROR_PATTERNS)
        recent_errors = docker.exec_cmd(
            cid,
            f"tail -n 200 /var/gerrit/logs/pull_replication_log 2>/dev/null | "
            f"grep -iE '{error_grep}'",
            check=False,
        )
        if recent_errors.strip():
            if debug:
                logger.debug("    Found replication errors in log")
            return False
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
