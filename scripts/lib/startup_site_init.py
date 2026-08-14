# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bootstrapping a Gerrit site with ``gerrit init``.

:func:`init_gerrit_site` runs the Gerrit image once, in ephemeral mode,
with ``init`` as its command so that the empty host directories laid
out by :mod:`startup_site_layout` are populated with a real site.

The module also owns the translation of the ``gerrit_init_args`` action
input into argv tokens, including turning a malformed value into a
:class:`~errors.ConfigError` that names the offending input instead of
letting a lexer error escape from the middle of the start-up path.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

from docker_manager import DockerManager
from errors import ConfigError, DockerError, GerritActionError
from startup_site_layout import GERRIT_SUBDIRS, chown_tree, gerrit_volume_mounts

logger = logging.getLogger(__name__)


def init_gerrit_site(
    docker: DockerManager,
    instance_dir: Path,
    slug: str,
    canonical_url: str,
    image: str,
    extra_init_args: str = "",
) -> None:
    """Initialise a Gerrit site directory using ``gerrit init``.

    Runs the Gerrit image with ``init`` as the command, mounting only
    the individual sub-directories (not the whole ``/var/gerrit``
    directory) so that ``/var/gerrit/bin`` from the image is preserved.

    Parameters
    ----------
    extra_init_args:
        Optional shell-style argument string to pass to ``gerrit init``
        (from the ``gerrit_init_args`` action input).  Parsed with
        ``shlex.split`` so callers can use the familiar
        whitespace-separated form (``--foo bar --baz=qux``) and quote
        values that contain spaces.  Each resulting token becomes its
        own argv element.  Empty strings are ignored.
    """
    logger.info("Initializing Gerrit site for %s…", slug)

    # Create sub-directories with Gerrit-compatible ownership
    for subdir in GERRIT_SUBDIRS:
        d = instance_dir / subdir
        d.mkdir(parents=True, exist_ok=True)

    chown_tree(instance_dir)

    # Build volumes: mount each sub-directory individually
    volumes = gerrit_volume_mounts(instance_dir)

    try:
        # Pass --batch so init never prompts, and
        # --install-all-plugins so the bundled plugins from the
        # image (hooks, download-commands, delete-project,
        # webhooks, singleusergroup, reviewnotes, etc.) get copied
        # into the mounted plugins/ directory.  Without this the
        # mount shadows the image's bundled plugins and Gerrit
        # starts without the hooks plugin — which silently breaks
        # G2P because Gerrit never invokes the wrapper scripts in
        # /var/gerrit/hooks/.
        command = ["init", "--batch", "--install-all-plugins"]
        command.extend(_extra_init_tokens(extra_init_args))

        docker.run_ephemeral(
            image,
            volumes=volumes,
            env={"CANONICAL_WEB_URL": canonical_url},
            command=command,
            timeout=180,
        )
    except DockerError as exc:
        raise GerritActionError(
            f"Failed to initialize Gerrit site for {slug}: {exc}"
        ) from exc

    logger.info("Gerrit site initialized ✅")


def _extra_init_tokens(extra_init_args: str) -> list[str]:
    """Split the ``gerrit_init_args`` input into argv tokens.

    Honour any user-supplied extras.  Parse with ``shlex.split`` so
    callers can use the familiar shell-style whitespace-separated form
    (``--foo bar --baz``) and still get quoting handled correctly for
    values that contain spaces.  Each token becomes its own argv
    element, matching the behaviour ``gerrit init`` expects.

    ``shlex.split`` raises ``ValueError`` on malformed input (e.g. an
    unbalanced quote).  Convert that to a ConfigError pointing at the
    offending action input so the user sees an actionable ``::error::``
    line instead of an unhelpful stack trace from deep inside the
    container start path.
    """
    if not extra_init_args.strip():
        return []

    try:
        extra_tokens = shlex.split(extra_init_args)
    except ValueError as exc:
        raise ConfigError(
            f"Invalid 'gerrit_init_args' input ({exc}): {extra_init_args!r}"
        ) from exc

    return [extra for extra in extra_tokens if extra]
