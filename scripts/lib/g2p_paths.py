# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Canonical filesystem layout inside a running Gerrit container.

Every G2P setup and self-test step addresses the same handful of
absolute paths inside the Gerrit container.  Collecting them here
gives a single place to change the layout and keeps the setup,
self-test and hook-wrapper modules free of duplicated string
literals that could drift apart.

Usage::

    from g2p_paths import G2P_INI_PATH, GERRIT_HOOKS_DIR
"""

from __future__ import annotations

GERRIT_HOME = "/var/gerrit"
"""Gerrit base directory inside the container."""

GERRIT_USER_HOME = "/var/gerrit"
"""Home directory of the ``gerrit`` user inside the container."""

GERRIT_HOOKS_DIR = f"{GERRIT_HOME}/hooks"
"""Directory where Gerrit looks for hook scripts."""

GERRIT_PLUGINS_DIR = f"{GERRIT_HOME}/plugins"
"""Directory where Gerrit loads plugin JARs from at startup."""

GERRIT_ETC_DIR = f"{GERRIT_HOME}/etc"
"""Gerrit configuration directory."""

GERRIT_REPLICATION_CONFIG = f"{GERRIT_ETC_DIR}/replication.config"
"""Path to the Gerrit replication config inside the container."""

G2P_CONFIG_DIR = f"{GERRIT_USER_HOME}/.config/gerrit_to_platform"
"""XDG-style config directory for gerrit_to_platform."""

G2P_INI_PATH = f"{G2P_CONFIG_DIR}/gerrit_to_platform.ini"
"""Path to the g2p INI config inside the container."""

G2P_REPLICATION_SYMLINK = f"{G2P_CONFIG_DIR}/replication.config"
"""Symlink inside the g2p config dir pointing to the Gerrit repl config."""

GERRIT_TOOLS_VENV_BIN = "/opt/gerrit-tools/bin"
"""Path to the g2p console-script binaries inside the container."""

GERRIT_LOGS_DIR = f"{GERRIT_HOME}/logs"
"""Gerrit logs directory inside the container."""

G2P_HOOK_LOG = f"{GERRIT_LOGS_DIR}/g2p-hooks.log"
"""Single log file capturing every G2P hook invocation made by Gerrit.

Written to by the wrapper script ``setup_g2p_hooks`` installs in
``/var/gerrit/hooks/`` so operators can reconstruct exactly which
hook fired, with what arguments, and what the underlying
``gerrit_to_platform`` console script printed and returned.  This
is the single source of truth for diagnosing whether a Gerrit
event reached the dispatcher.
"""

SSH_DIR = f"{GERRIT_USER_HOME}/.ssh"
"""SSH directory inside the container."""
