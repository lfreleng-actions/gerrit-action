# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Thin wrapper around the Docker CLI via :mod:`subprocess`.

All Docker interaction in the project goes through this single module,
providing:

- Structured error handling via :class:`DockerError`
- Consistent timeout management
- Debug logging of every command
- A single point of change if Docker CLI output formats evolve

We deliberately use the Docker CLI rather than the Docker SDK
(``docker-py``) to avoid adding a heavy dependency.  The CLI is always
available in GitHub Actions runners.

:class:`DockerManager` is the public facade.  The commands themselves
are grouped by the part of the Docker CLI they drive, so each group can
be read and tested on its own:

- :mod:`docker_cli` — running a ``docker`` process and translating
  failures into :class:`DockerError` (the base every group builds on)
- :mod:`docker_images` — image existence, build and pull
- :mod:`docker_containers` — container lifecycle (run, stop, kill, rm)
- :mod:`docker_inspect` — read-only container state and logs
- :mod:`docker_exec` — ``docker exec`` and ``docker cp``
- :mod:`docker_system` — daemon-wide queries and housekeeping

Usage::

    from docker_manager import DockerManager

    docker = DockerManager()
    docker.run_container(
        image="gerrit-extended:3.13.1-ubuntu24",
        name="gerrit-test",
        ports={18080: 8080, 29418: 29418},
    )
    logs = docker.container_logs("gerrit-test", tail=100)
    docker.stop("gerrit-test")
    docker.remove("gerrit-test")
"""

from __future__ import annotations

from docker_cli import DockerCommandRunner
from docker_containers import DockerContainerCommands
from docker_exec import DockerExecCommands
from docker_images import DockerImageCommands
from docker_inspect import DockerInspectCommands
from docker_system import DockerSystemCommands

__all__ = ["DockerCommandRunner", "DockerManager"]


class DockerManager(
    DockerImageCommands,
    DockerContainerCommands,
    DockerInspectCommands,
    DockerExecCommands,
    DockerSystemCommands,
):
    """Thin wrapper around the Docker CLI.

    Every public method translates its arguments into a ``docker …``
    command, runs it via :func:`subprocess.run`, and either returns the
    result or raises :class:`DockerError` with full diagnostic context.

    The methods are contributed by the per-area command classes listed
    in the module docstring; all of them share the single
    :meth:`~docker_cli.DockerCommandRunner.run_cmd` implementation, so
    timeout and error handling are uniform across the whole surface.
    """
