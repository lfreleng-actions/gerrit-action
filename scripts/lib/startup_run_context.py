# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The run-wide collaborators shared by every instance being started.

A single invocation of the start-up orchestrator provisions one or more
instances, but the Docker wrapper, the action config, the two JSON
stores and the resolved image tag are established once and are the same
for all of them.  :class:`StartupRunContext` names that set so the
per-instance functions take only what actually varies — the instance
and its index — instead of threading five unchanging arguments through
every call.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import ActionConfig, ApiPathStore, InstanceStore
from docker_manager import DockerManager


@dataclass(frozen=True)
class StartupRunContext:
    """Collaborators established once per start-up run.

    Attributes
    ----------
    docker:
        Docker command wrapper used for every container operation.
    config:
        Validated action configuration for the whole run.
    api_path_store:
        API paths recorded by the earlier detection phase, keyed by slug.
    instance_store:
        Destination for the ``instances.json`` metadata written as each
        instance comes up.
    image:
        Tag of the Gerrit image all instances are started from.
    """

    docker: DockerManager
    config: ActionConfig
    api_path_store: ApiPathStore
    instance_store: InstanceStore
    image: str
