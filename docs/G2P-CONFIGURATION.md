<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2025 The Linux Foundation -->

# G2P Configuration: Development Plan

> **gerrit_to_platform** plugin integration for
> `gerrit-action`

This document serves as the comprehensive development plan
for adding `gerrit_to_platform` (G2P) configuration support
to `gerrit-action`. The goal: enable a Gerrit instance
deployed in a GitHub CI environment via `gerrit-action` to
communicate with a GitHub Organisation and execute GitHub
Actions workflows in that target organisation.

## Table of Contents

- [Background and Context](#background-and-context)
  - [What is gerrit_to_platform?](#what-is-gerrit_to_platform)
  - [How gerrit_to_platform Works](#how-gerrit_to_platform-works)
  - [Production Configuration from Puppet/Hiera](#production-configuration-from-puppethiera)
  - [Current gerrit-action Architecture](#current-gerrit-action-architecture)
- [Required Configuration Elements](#required-configuration-elements)
  - [Gerrit-Side Configuration](#gerrit-side-configuration)
  - [GitHub-Side Configuration](#github-side-configuration)
  - [Configuration File Mapping](#configuration-file-mapping)
- [Action Inputs Design](#action-inputs-design)
  - [Primary Enable Flag](#primary-enable-flag)
  - [GitHub Credentials](#github-credentials)
  - [Replication Mapping](#replication-mapping)
  - [Comment-Added Mappings](#comment-added-mappings)
  - [Hook Selection](#hook-selection)
  - [Validation Behaviour](#validation-behaviour)
  - [Complete Input Reference](#complete-input-reference)
- [Action Outputs Design](#action-outputs-design)
- [Implementation Plan](#implementation-plan)
  - [Phase 1 — Configuration Model](#phase-1--configuration-model)
  - [Phase 2 — G2P Config File Generation](#phase-2--g2p-config-file-generation)
  - [Phase 3 — Hook Symlink Management](#phase-3--hook-symlink-management)
  - [Phase 4 — GitHub Checks](#phase-4--github-checks)
  - [Phase 5 — Action Integration](#phase-5--action-integration)
  - [Phase 6 — Tests](#phase-6--tests)
- [Detailed Module Specifications](#detailed-module-specifications)
  - [G2P Config Module](#g2p-config-module)
  - [G2P Setup Script](#g2p-setup-script)
  - [GitHub Checks Module](#github-checks-module)
  - [SSH Key Generation](#ssh-key-generation)
- [Auto-Generation of Defaults](#auto-generation-of-defaults)
- [Error Handling and Check Strategy](#error-handling-and-check-strategy)
  - [Check Levels](#check-levels)
  - [Check Order](#check-order)
  - [Error Propagation](#error-propagation)
  - [G2P Exception Classes](#g2p-exception-classes)
- [Integration with Existing Architecture](#integration-with-existing-architecture)
  - [Where G2P Fits in the Action Flow](#where-g2p-fits-in-the-action-flow)
  - [Integration Points](#integration-points)
  - [Environment Variable Flow](#environment-variable-flow)
- [Security Considerations](#security-considerations)
  - [Secrets Handling](#secrets-handling)
  - [File Permissions](#file-permissions)
  - [Token Scope — Least Privilege](#token-scope--least-privilege)
- [File Tree of Changes](#file-tree-of-changes)
- [Testing Strategy](#testing-strategy)
  - [Unit Tests](#unit-tests)
  - [Mock Strategy](#mock-strategy)
  - [Integration Scenarios](#integration-scenarios)
  - [Test Fixtures](#test-fixtures)
- [Future Considerations](#future-considerations)
- [Appendix A — Example Usage](#appendix-a--example-usage)
- [Appendix B — gerrit_to_platform.ini Reference](#appendix-b--gerrit_to_platformini-reference)
- [Appendix C — Replication Config G2P Section](#appendix-c--replication-config-g2p-section)
- [Appendix D — Gerrit Hook Symlinks](#appendix-d--gerrit-hook-symlinks)

## Background and Context

### What is gerrit_to_platform?

`gerrit_to_platform` is a Python package that bridges Gerrit
Code Review and CI/CD platforms (primarily GitHub Actions).
Gerrit hook entry points invoke this tool when events occur
(patchset uploaded, comment added, change merged),
dispatching corresponding GitHub Actions `workflow_dispatch`
events on the appropriate repositories via the GitHub API.

Source: `gerrit.linuxfoundation.org` (mirrored at
`github.com/lfit/releng-gerrit_to_platform`)

Package version in Docker image: Pinned in
`docker/requirements.txt` via Dependabot.

### How gerrit_to_platform Works

The tool requires two configuration files under
`~gerrituser/.config/gerrit_to_platform/`:

- `gerrit_to_platform.ini` — App config containing
  GitHub/GitLab tokens and comment keyword-to-workflow
  mappings
- `replication.config` — Gerrit replication remote
  definitions (symlinked from Gerrit's own config)

Event flow:

```text
Gerrit hook fires (e.g. patchset-created)
  → gerrit_to_platform CLI entry point
  → Parse replication.config for GitHub remotes/owners
  → For each remote: convert repo name, filter, dispatch
  → Check .github magic repo for "required" workflows
  → Return dispatched count
```

Three entry points (console scripts):

- `patchset-created` — search filter: `verify`
- `change-merged` — search filter: `merge`
- `comment-added` — config mapping or ChatOps `gha-*`

GitHub workflow requirements (from g2p README):

- Workflow filename MUST contain `gerrit`
- Workflow filename MUST contain the search filter
  (e.g. `verify`)
- Required (org-wide) workflows go in `ORG/.github` repo
- Required workflow filenames MUST also contain `required`
- All workflows accept 9 standard `GERRIT_*` inputs via
  `workflow_dispatch`
- Required workflows accept a `TARGET_REPO` input

Replication config platform detection — the system determines
GitHub vs GitLab by checking if `"github"` or `"gitlab"`
appears in:

1. The section name
2. The subsection name (remote identifier)
3. The `authGroup` value

Repo name conversion (`remoteNameStyle`):

- `slash` — no conversion (`releng/lftools`)
- `dash` — slash to dash (`releng-lftools`)
- `underscore` — slash to underscore (`releng_lftools`)

### Production Configuration from Puppet/Hiera

Analysis of production Gerrit hiera data reveals patterns for
configuring `gerrit_to_platform` across LF projects (ONAP,
FD.io, ODL, ORAN, etc.):

**G2P Config** (via `gerrit::gerrit_to_platform::config`):

```yaml
gerrit::gerrit_to_platform::config:
  'mapping "comment-added"':
    recheck: verify
    remerge: merge
    'rerun-gha': verify
    'remerge-gha': merge
  "github.com":
    token: ENC[GPG,...]  # Encrypted GitHub PAT
```

**Plugin List** (always includes `hooks` and `replication`):

```yaml
gerrit::plugin_list:
  - commit-message-length-validator
  - delete-project
  - download-commands
  - hooks           # REQUIRED for g2p
  - replication      # REQUIRED for g2p
  - reviewnotes
  - webhooks         # Some instances (ODL)
```

**SSH Known Hosts for GitHub:**

```yaml
ssh::known_hosts:
  'github.com':
    name: 'github.com'
    host_aliases:
      - 192.30.252.131
    ensure: present
    type: 'ssh-ed25519'
    key: 'AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnk...'
```

**SSH Client Config** (gerrit user → <git@github.com>):

```yaml
ssh::users_client_options:
  gerrit:
    user_home_dir: '/opt/gerrit'
    options:
      'Host github.com':
        User: git
```

**Replication Config** (push-based, in production):

```yaml
gerrit::extra_configs:
  replication_config:
    config_file: '/opt/gerrit/etc/replication.config'
    options:
      'gerrit':
        autoReload: true
      'remote.github':
        url: 'git@github.com:org/${name}.git'
        push:
          - '+refs/*:refs/*'
        timeout: '5'
        threads: '5'
        authGroup: 'GitHub Replication'
        remoteNameStyle: 'dash'
```

Key insights from production:

- The GitHub token is a Fine-Grained PAT scoped to the org
  with Actions read+write, Contents read, and Metadata read
  permissions
- Hook symlinks connect installed console scripts to
  Gerrit's hook directory
- The replication.config file is symlinked into the g2p
  config directory
- SSH keys for <git@github.com> access live separate from
  g2p config

### Current gerrit-action Architecture

`gerrit-action` provisions Docker-based Gerrit containers
with pull-replication in GitHub Actions using:

- Thin CLI scripts under `scripts/` as entry points
- Library modules under `scripts/lib/` for business logic
- Frozen dataclasses (`ActionConfig`, `InstanceConfig`,
  `TunnelConfig`) for typed, validated configuration
- `InstanceStore`/`ApiPathStore` for JSON persistence
- Custom exception hierarchy rooted at `GerritActionError`
- Docker subprocess wrapper (`DockerManager`) for
  container ops

The Dockerfile already installs `gerrit-to-platform` via
`docker/requirements.txt`, providing console script entry
points (`change-merged`, `comment-added`,
`patchset-created`) inside the container.

What the action lacks today — although the package exists in
the image, there is no mechanism to:

1. Generate the `gerrit_to_platform.ini` config file
2. Symlink the `replication.config` into the g2p config
   directory
3. Create Gerrit hook symlinks to the console scripts
4. Provide or generate a GitHub token
5. Configure SSH known hosts for `github.com`
6. Check that the target GitHub org has correct setup

## Required Configuration Elements

### Gerrit-Side Configuration

Everything that the action must set up inside the Docker
container:

<!-- markdownlint-disable MD013 -->

| #   | Element                      | Location in Container                                                                          | Purpose                                           |
| --- | ---------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| 1   | `gerrit_to_platform.ini`     | `~gerrit/.config/gerrit_to_platform/gerrit_to_platform.ini`                                    | App config with GitHub token and comment mappings |
| 2   | `replication.config` symlink | `~gerrit/.config/gerrit_to_platform/replication.config` → `/var/gerrit/etc/replication.config` | Platform detection via replication remotes        |
| 3   | Hook symlinks                | `/var/gerrit/hooks/patchset-created` → `$GERRIT_TOOLS_VENV/bin/patchset-created` (etc.)        | Connect Gerrit events to g2p handlers             |
| 4   | `hooks` plugin               | `/var/gerrit/plugins/hooks.jar`                                                                | Already bundled; enables hook execution           |
| 5   | SSH known hosts              | `~gerrit/.ssh/known_hosts`                                                                     | Contains `github.com` host key                    |
| 6   | SSH client config            | `~gerrit/.ssh/config`                                                                          | `Host github.com` with `User git`                 |
| 7   | SSH private key              | `~gerrit/.ssh/id_rsa`                                                                          | For push-based replication to GitHub              |
| 8   | `replication.config`         | `/var/gerrit/etc/replication.config`                                                           | Must contain `remote.github` sections             |

<!-- markdownlint-enable MD013 -->

Notes:

- Items 5-7 are partially handled by the existing SSH setup
  for pull-replication. The action must augment (not
  replace) those files.
- The `hooks` plugin already ships in the official Gerrit
  Docker image.
- The `replication.config` already exists for
  pull-replication; it needs GitHub remote info that g2p
  uses for platform detection.

### GitHub-Side Configuration

Elements that must exist in the target GitHub Organisation:

<!-- markdownlint-disable MD013 -->

| #   | Element            | Where                             | Purpose                                | Checkable? |
| --- | ------------------ | --------------------------------- | -------------------------------------- | ---------- |
| 1   | Fine-Grained PAT   | Org Settings → Tokens             | Authenticate g2p API calls             | Yes        |
| 2   | PAT permissions    | Token config                      | Actions: R/W, Contents: R, Metadata: R | Yes        |
| 3   | Org token policy   | Org Settings                      | Must allow fine-grained tokens         | Partial    |
| 4   | `ORG/.github` repo | GitHub Org                        | Houses required workflows              | Yes        |
| 5   | Gerrit workflows   | `.github/workflows/gerrit-*.yaml` | Respond to `workflow_dispatch`         | Yes        |
| 6   | Workflow inputs    | Each workflow file                | Must accept 9 `GERRIT_*` inputs        | Partial    |
| 7   | SSH deploy keys    | Repo settings                     | For push-based replication             | Yes        |

<!-- markdownlint-enable MD013 -->

### Configuration File Mapping

<!-- markdownlint-disable MD013 -->

| Config Element  | g2p Config File          | INI Section                             | Source                          |
| --------------- | ------------------------ | --------------------------------------- | ------------------------------- |
| GitHub token    | `gerrit_to_platform.ini` | `[github.com]` → `token`                | `g2p_github_token` input        |
| GitLab token    | `gerrit_to_platform.ini` | `[gitlab.com]` → `token`                | Future: `g2p_gitlab_token`      |
| Comment recheck | `gerrit_to_platform.ini` | `[mapping "comment-added"]`             | `g2p_comment_mappings`          |
| Remote URL      | `replication.config`     | `[remote "github"]` → `url`             | Derived from `g2p_github_owner` |
| Name style      | `replication.config`     | `[remote "github"]` → `remoteNameStyle` | `g2p_remote_name_style`         |
| Auth group      | `replication.config`     | `[remote "github"]` → `authGroup`       | Auto-generated                  |

<!-- markdownlint-enable MD013 -->

## Action Inputs Design

All new inputs share the `g2p_` prefix.

### Primary Enable Flag

```yaml
g2p_enable:
  description: |
    Enable Gerrit to Platform (g2p) integration.
    When true, configures the gerrit_to_platform plugin
    inside the Gerrit container to dispatch GitHub Actions
    workflows in response to Gerrit events.
  required: false
  default: 'false'
```

When `g2p_enable` is `false` (default), the action ignores
all other `g2p_*` inputs. This preserves full backward
compatibility.

### GitHub Credentials

```yaml
g2p_github_token:
  description: |
    GitHub Personal Access Token (Fine-Grained) for the
    target organisation. Required permissions: Actions
    (read/write), Contents (read), Metadata (read).
    When not provided, the action outputs a warning and
    generates the g2p config WITHOUT a token. This allows
    the container setup to proceed when the GitHub org
    will receive configuration later.
  required: false
  default: ''

g2p_github_owner:
  description: |
    GitHub organisation or user that owns the target
    repositories. Used to construct the replication remote
    URL pattern and for GitHub API checks.
    Example: "onap", "fdio", "opendaylight"
    REQUIRED when g2p_enable is true.
  required: false
  default: ''
```

### Replication Mapping

```yaml
g2p_remote_name_style:
  description: |
    How Gerrit project names map to GitHub repository
    names. Controls remoteNameStyle in the replication
    config that g2p reads for platform detection.
    - dash: releng/lftools → releng-lftools (most common)
    - underscore: releng/lftools → releng_lftools
    - slash: releng/lftools → releng/lftools
  required: false
  default: 'dash'

g2p_remote_url:
  description: |
    Override the GitHub remote URL pattern for the
    replication config. Uses ${name} as the repository
    name placeholder. When not provided, auto-generated
    as: git@github.com:<g2p_github_owner>/${name}.git
    Override this for non-standard URL patterns.
  required: false
  default: ''

g2p_remote_auth_group:
  description: |
    The Gerrit authGroup for the GitHub replication
    remote. Must contain "github" (case-insensitive) for
    g2p platform detection.
  required: false
  default: 'GitHub Replication'
```

### Comment-Added Mappings

```yaml
g2p_comment_mappings:
  description: |
    JSON object mapping comment keywords to workflow
    search filters for the comment-added hook. Defines
    which Gerrit comment keywords trigger which GitHub
    workflow types. Default provides standard LF mappings.
    Example: {"recheck": "verify", "remerge": "merge"}
  required: false
  default: >-
    {"recheck": "verify", "reverify": "verify",
     "remerge": "merge"}
```

### Hook Selection

```yaml
g2p_hooks:
  description: |
    Comma-separated list of Gerrit hooks to enable for
    g2p. Each hook gets a symlink in the container's
    hooks directory pointing to the g2p console script.
    Available: patchset-created, comment-added,
               change-merged
  required: false
  default: 'patchset-created,comment-added,change-merged'
```

### Validation Behaviour

```yaml
g2p_validation_mode:
  description: |
    Controls behaviour when GitHub-side checks fail.
    - error: Fail the action if checks fail (strict)
    - warn: Log warnings but continue (lenient, for
            when GitHub org gets configured later)
    - skip: Skip all GitHub-side checks entirely
  required: false
  default: 'warn'

g2p_validate_workflows:
  description: |
    When true and g2p_validation_mode is not 'skip',
    checks that the target GitHub org has properly
    configured Gerrit workflows (filenames containing
    'gerrit' and the appropriate search filter).
    Checks the .github magic repo and individual repos
    when g2p_validate_repos has entries.
  required: false
  default: 'true'

g2p_validate_repos:
  description: |
    Comma-separated list of GitHub repositories to check
    for proper Gerrit workflow configuration. When empty,
    the action checks the .github magic repo alone.
    Example: "ci-management,releng-lftools"
  required: false
  default: ''
```

### Complete Input Reference

<!-- markdownlint-disable MD013 -->

| Input                    | Type   | Default              | Required | Purpose            |
| ------------------------ | ------ | -------------------- | -------- | ------------------ |
| `g2p_enable`             | bool   | `false`              | No       | Master enable flag |
| `g2p_github_token`       | secret | `''`                 | When on  | GitHub PAT         |
| `g2p_github_owner`       | string | `''`                 | When on  | Target org/user    |
| `g2p_remote_name_style`  | enum   | `dash`               | No       | Repo name style    |
| `g2p_remote_url`         | string | `''`                 | No       | Override URL       |
| `g2p_remote_auth_group`  | string | `GitHub Replication` | No       | Auth group         |
| `g2p_comment_mappings`   | JSON   | see above            | No       | Keyword → filter   |
| `g2p_hooks`              | CSV    | all three            | No       | Hook selection     |
| `g2p_validation_mode`    | enum   | `warn`               | No       | Check behaviour    |
| `g2p_validate_workflows` | bool   | `true`               | No       | Check workflows    |
| `g2p_validate_repos`     | CSV    | `''`                 | No       | Repos to check     |
| `g2p_ssh_private_key`    | secret | `''`                 | No       | SSH key for GitHub |
| `g2p_github_known_hosts` | string | `''`                 | No       | SSH known hosts    |

<!-- markdownlint-enable MD013 -->

## Action Outputs Design

New outputs when g2p runs:

| Output                   | Format      | Description                   |
| ------------------------ | ----------- | ----------------------------- |
| `g2p_enabled`            | bool string | Whether g2p ran               |
| `g2p_config_path`        | string      | Path to generated INI         |
| `g2p_hooks_enabled`      | JSON array  | Which hooks got symlinks      |
| `g2p_github_owner`       | string      | Configured GitHub owner       |
| `g2p_remote_name_style`  | string      | Configured name style         |
| `g2p_validation_results` | JSON        | GitHub check results          |
| `g2p_token_provided`     | bool string | Whether token exists          |
| `g2p_ssh_public_key`     | string      | Public key for downstream use |

The `g2p_ssh_public_key` output matters most when no SSH key
input arrives: the action auto-generates a keypair and
outputs the public key so a downstream workflow step can add
it as a deploy key to the appropriate GitHub repositories.

## Implementation Plan

### Phase 1 — Configuration Model

New file: `scripts/lib/g2p_config.py`

Create a frozen dataclass `G2PConfig` that models all
g2p-related configuration with validation:

```python
@dataclass(frozen=True)
class G2PConfig:
    enabled: bool
    github_token: str
    github_owner: str
    remote_name_style: str       # dash, underscore, slash
    remote_url: str              # auto-generated if empty
    remote_auth_group: str
    comment_mappings: dict       # keyword → filter
    hooks: list                  # list of hook names
    validation_mode: str         # error, warn, skip
    validate_workflows: bool
    validate_repos: list         # list of repo names
    ssh_private_key: str
    github_known_hosts: str
```

Factory method: `G2PConfig.from_environment()` reads from
environment variables (set by the action step).

Checks performed by `G2PConfig.check()`:

- If enabled, `github_owner` must have a value
- `remote_name_style` must be dash, underscore, or slash
- `validation_mode` must be error, warn, or skip
- `hooks` must be a subset of known hooks
- `comment_mappings` must parse as valid JSON
- If `github_token` is empty, emit a warning

### Phase 2 — G2P Config File Generation

New file: `scripts/lib/g2p_setup.py`

Functions to generate and deploy config files inside the
container:

`generate_g2p_ini(config)` — Produces the
`gerrit_to_platform.ini` content:

```ini
[mapping "comment-added"]
recheck = verify
reverify = verify
remerge = merge

[github.com]
token = <token_value>
```

`generate_g2p_replication_remote(config)` — Produces the
replication config section that g2p needs for platform
detection. This gets appended to the existing
`replication.config`:

```ini
# G2P platform detection remote (not for replication)
[remote "github-g2p"]
  url = git@github.com:onap/${name}.git
  authGroup = GitHub Replication
  remoteNameStyle = dash
```

Important design note: In `gerrit-action`, the existing
`replication.config` handles pull-replication (fetching from
a remote Gerrit into the local container). The g2p plugin
reads this same file differently — it extracts the GitHub
remote URL pattern, owner, and name style to know where to
dispatch workflows. The g2p detection remote must live
alongside the pull-replication remote.

`setup_g2p_config_dir(instance_dir, config)`:

1. Create `~gerrit/.config/gerrit_to_platform/`
2. Write `gerrit_to_platform.ini`
3. Symlink `replication.config` →
   `<instance_dir>/etc/replication.config`

`setup_g2p_hooks(config)` — For each enabled hook, create
a symlink:

```text
/var/gerrit/hooks/patchset-created
  → /opt/gerrit-tools/bin/patchset-created
/var/gerrit/hooks/comment-added
  → /opt/gerrit-tools/bin/comment-added
/var/gerrit/hooks/change-merged
  → /opt/gerrit-tools/bin/change-merged
```

`setup_g2p_ssh(config)`:

1. If `g2p_ssh_private_key` has a value, write it to
   `~gerrit/.ssh/g2p_github_key`
2. Otherwise, generate an Ed25519 keypair
3. Append `github.com` to known_hosts (auto-scan if absent)
4. Append SSH client config for `Host github.com`
5. Return the public key for output

### Phase 3 — Hook Symlink Management

Hook symlinks go inside the running container after Gerrit
starts (or during init). Use `docker exec` via
`DockerManager`:

```python
def create_hook_symlink(
    docker: DockerManager,
    container_id: str,
    hook_name: str,
    target_bin: str,
) -> None:
    """Create a Gerrit hook symlink inside container."""
    hook_path = f"/var/gerrit/hooks/{hook_name}"
    docker.exec_command(
        container_id,
        ["ln", "-sf", target_bin, hook_path],
        user="gerrit",
    )
```

### Phase 4 — GitHub Checks

New file: `scripts/lib/g2p_github.py`

This module performs GitHub API checks using the `requests`
library (already in `GERRIT_SCRIPTS_VENV`). We avoid `ghapi`
here to keep the scripts-side dependency minimal and use the
same HTTP patterns as the rest of `gerrit-action`.

GitHub REST API Checks:

<!-- markdownlint-disable MD013 -->

| Check           | API Endpoint                                  | What It Verifies        |
| --------------- | --------------------------------------------- | ----------------------- |
| Token validity  | `GET /user`                                   | Returns 200, auth works |
| Token scope     | `GET /orgs/{owner}`                           | Confirms org access     |
| Org exists      | `GET /orgs/{owner}`                           | Returns 200             |
| `.github` repo  | `GET /repos/{owner}/.github`                  | Returns 200             |
| Workflows       | `GET /repos/{owner}/{repo}/actions/workflows` | Active gerrit workflows |
| Workflow inputs | `GET /repos/{owner}/{repo}/contents/...`      | Parse YAML for inputs   |
| Deploy keys     | `GET /repos/{owner}/{repo}/keys`              | SSH key present         |

<!-- markdownlint-enable MD013 -->

GitHub GraphQL API — for efficient bulk queries:

```graphql
query ValidateOrgRepos(
  $owner: String!, $repos: [String!]!
) {
  organization(login: $owner) {
    repositories(first: 100, names: $repos) {
      nodes {
        name
        isArchived
        defaultBranchRef { name }
      }
    }
  }
}
```

Check result model:

```python
@dataclass
class G2PCheckResult:
    check_name: str
    passed: bool
    message: str
    severity: str  # error, warning, info
```

Check runner:

```python
def check_github_config(
    config: G2PConfig,
    mode: str,  # "error", "warn", "skip"
) -> list[G2PCheckResult]:
    """Run all GitHub checks."""
```

The runner collects results and, based on `mode`:

- `error`: Any failed check raises `G2PCheckError`
- `warn`: Failed checks emit `::warning::` annotations
- `skip`: Returns empty list

### Phase 5 — Action Integration

Modified file: `action.yaml` — Add all `g2p_*`
inputs/outputs. Add a new step:

```yaml
- name: Configure G2P
  if: inputs.g2p_enable == 'true'
  id: configure-g2p
  shell: bash
  env:
    G2P_ENABLE: ${{ inputs.g2p_enable }}
    G2P_GITHUB_TOKEN: ${{ inputs.g2p_github_token }}
    G2P_GITHUB_OWNER: ${{ inputs.g2p_github_owner }}
    # ... all other G2P_* env vars ...
    DEBUG: ${{ inputs.debug }}
  run: |
    "$GERRIT_SCRIPTS_VENV/bin/python" \
      "${{ github.action_path }}/scripts/configure-g2p.py"
```

This step runs after `start-instances` and `check-services`
but before `collect-outputs`, so g2p outputs appear in the
final collection.

Modified file: `scripts/start-instances.py` — In
`generate_replication_config()`, when g2p has activation,
append the g2p platform detection remote.

Modified file: `scripts/collect-outputs.py` — Collect g2p
outputs and include them in `$GITHUB_OUTPUT`.

### Phase 6 — Tests

New test files:

- `tests/test_g2p_config.py` — G2PConfig dataclass checks
- `tests/test_g2p_setup.py` — Config file generation
- `tests/test_g2p_github.py` — GitHub checks (mocked)

Coverage targets:

- G2P config: 100%
- Config file generation: 100%
- GitHub API: 90% (mocked HTTP responses)
- Hook symlinks: 80% (Docker exec mocked)

## Detailed Module Specifications

### G2P Config Module

File: `scripts/lib/g2p_config.py`

```python
"""G2P configuration model."""

import json
import os
from dataclasses import dataclass, field

from scripts.lib.config import ConfigError
from scripts.lib.logging_utils import get_logger

VALID_NAME_STYLES = ("dash", "underscore", "slash")
VALID_CHECK_MODES = ("error", "warn", "skip")
VALID_HOOKS = (
    "patchset-created",
    "comment-added",
    "change-merged",
)
DEFAULT_COMMENT_MAPPINGS = {
    "recheck": "verify",
    "reverify": "verify",
    "remerge": "merge",
}


@dataclass(frozen=True)
class G2PConfig:
    enabled: bool = False
    github_token: str = ""
    github_owner: str = ""
    remote_name_style: str = "dash"
    remote_url: str = ""
    remote_auth_group: str = "GitHub Replication"
    comment_mappings: dict = field(
        default_factory=lambda: dict(
            DEFAULT_COMMENT_MAPPINGS
        )
    )
    hooks: list = field(
        default_factory=lambda: list(VALID_HOOKS)
    )
    validation_mode: str = "warn"
    validate_workflows: bool = True
    validate_repos: list = field(
        default_factory=list
    )
    ssh_private_key: str = ""
    github_known_hosts: str = ""

    @classmethod
    def from_environment(cls) -> "G2PConfig":
        """Build config from environment variables."""
        ...

    def check(self) -> list[str]:
        """Check config, return list of errors."""
        ...

    @property
    def effective_remote_url(self) -> str:
        """Return remote URL, auto-generating if empty."""
        if self.remote_url:
            return self.remote_url
        if self.github_owner:
            return (
                f"git@github.com:{self.github_owner}"
                "/${name}.git"
            )
        return ""
```

### G2P Setup Script

File: `scripts/configure-g2p.py`

```python
"""Configure gerrit_to_platform inside containers."""

import sys

from lib.g2p_config import G2PConfig
from lib.g2p_setup import setup_g2p_all_instances
from lib.g2p_github import check_github_config
from lib.logging_utils import (
    configure_logging,
    get_logger,
)


def main() -> int:
    """Configure G2P for all running instances."""
    configure_logging()
    log = get_logger(__name__)

    try:
        config = G2PConfig.from_environment()
        errors = config.check()
        if errors:
            for err in errors:
                log.error(
                    "G2P config error: %s", err
                )
            return 1

        # Phase 1: GitHub checks
        if config.validation_mode != "skip":
            results = check_github_config(config)
            # Handle results based on validation_mode

        # Phase 2: Configure each instance
        setup_g2p_all_instances(config)

        # Phase 3: Write outputs

        return 0

    except Exception as e:
        log.error(
            "G2P configuration failed: %s", e
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

### GitHub Checks Module

File: `scripts/lib/g2p_github.py`

```python
"""GitHub API checks for g2p configuration."""

import json
from dataclasses import dataclass
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


GITHUB_API_BASE = "https://api.github.com"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

REQUIRED_WORKFLOW_INPUTS = [
    "GERRIT_BRANCH",
    "GERRIT_CHANGE_ID",
    "GERRIT_CHANGE_NUMBER",
    "GERRIT_CHANGE_URL",
    "GERRIT_EVENT_TYPE",
    "GERRIT_PATCHSET_NUMBER",
    "GERRIT_PATCHSET_REVISION",
    "GERRIT_PROJECT",
    "GERRIT_REFSPEC",
]


@dataclass
class G2PCheckResult:
    check_name: str
    passed: bool
    message: str
    severity: str  # error, warning, info


def check_token(token: str) -> G2PCheckResult:
    """Check token via GET /user."""
    ...


def check_org_access(
    token: str, owner: str
) -> G2PCheckResult:
    """Check token can access the target org."""
    ...


def check_magic_repo(
    token: str, owner: str
) -> G2PCheckResult:
    """Check .github repo exists in org."""
    ...


def check_workflows(
    token: str,
    owner: str,
    repo: str,
    search_filter: str,
) -> G2PCheckResult:
    """Check repo has gerrit workflows."""
    ...


def check_github_config(
    config: "G2PConfig",
) -> list[G2PCheckResult]:
    """Run all applicable GitHub checks."""
    results = []

    if not config.github_token:
        results.append(G2PCheckResult(
            check_name="token_provided",
            passed=False,
            message=(
                "No GitHub token provided; g2p "
                "cannot dispatch workflows"
            ),
            severity="warning",
        ))
        return results  # Cannot check further

    results.append(
        check_token(config.github_token)
    )
    results.append(
        check_org_access(
            config.github_token,
            config.github_owner,
        )
    )
    results.append(
        check_magic_repo(
            config.github_token,
            config.github_owner,
        )
    )

    if config.validate_workflows:
        for sf in ("verify", "merge"):
            results.append(
                check_workflows(
                    config.github_token,
                    config.github_owner,
                    ".github",
                    sf,
                )
            )

        for repo in config.validate_repos:
            for sf in ("verify", "merge"):
                results.append(
                    check_workflows(
                        config.github_token,
                        config.github_owner,
                        repo,
                        sf,
                    )
                )

    return results
```

### SSH Key Generation

Within `scripts/lib/g2p_setup.py`:

```python
def generate_ssh_keypair() -> tuple[str, str]:
    """Generate an Ed25519 SSH keypair for g2p.

    Returns:
        Tuple of (private_key, public_key) as strings.
    """
    import subprocess

    result = subprocess.run(
        [
            "ssh-keygen",
            "-t", "ed25519",
            "-f", "/tmp/g2p_key",
            "-N", "",
            "-C", "gerrit-action-g2p",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    with open("/tmp/g2p_key") as f:
        private_key = f.read()
    with open("/tmp/g2p_key.pub") as f:
        public_key = f.read()

    os.unlink("/tmp/g2p_key")
    os.unlink("/tmp/g2p_key.pub")

    return private_key, public_key


def fetch_github_host_keys() -> str:
    """Fetch GitHub SSH host keys via ssh-keyscan."""
    import subprocess

    result = subprocess.run(
        [
            "ssh-keyscan",
            "-t", "ed25519,rsa",
            "github.com",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    # Fallback to well-known GitHub Ed25519 key
    return (
        "github.com ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAI"
        "OMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    )
```

## Auto-Generation of Defaults

When `g2p_enable` is `true` but optional inputs lack values,
the action auto-generates sensible defaults:

<!-- markdownlint-disable MD013 -->

| Missing Input            | Generated Value                      | Output                     |
| ------------------------ | ------------------------------------ | -------------------------- |
| `g2p_github_token`       | None; g2p config lacks token         | `g2p_token_provided=false` |
| `g2p_remote_url`         | `git@github.com:<owner>/${name}.git` | `g2p_remote_url`           |
| `g2p_comment_mappings`   | Standard LF mappings                 | In config                  |
| `g2p_hooks`              | All three hooks                      | `g2p_hooks_enabled`        |
| `g2p_ssh_private_key`    | New Ed25519 keypair                  | `g2p_ssh_public_key`       |
| `g2p_github_known_hosts` | Via `ssh-keyscan github.com`         | In container               |
| `g2p_remote_auth_group`  | `GitHub Replication`                 | In replication config      |

<!-- markdownlint-enable MD013 -->

The `g2p_ssh_public_key` output matters most here: workflows
consuming `gerrit-action` can use it to configure the target
GitHub org (e.g. adding deploy keys to repositories via the
GitHub API).

## Error Handling and Check Strategy

### Check Levels

The `g2p_validation_mode` input controls failure handling:

| Mode    | On Failure                   | Use Case                   |
| ------- | ---------------------------- | -------------------------- |
| `error` | Emit `::error::`, fail step  | Production: org must work  |
| `warn`  | Emit `::warning::`, continue | Dev: org gets config later |
| `skip`  | No checks at all             | Offline environments       |

### Check Order

| #   | Check                     | Needs Token | On Failure |
| --- | ------------------------- | ----------- | ---------- |
| 1   | Token exists              | No          | warning    |
| 2   | Token works (`GET /user`) | Yes         | error      |
| 3   | Org accessible            | Yes         | error      |
| 4   | `.github` repo exists     | Yes         | warning    |
| 5   | Required workflows exist  | Yes         | warning    |
| 6   | Per-repo workflows        | Yes         | info       |
| 7   | Workflow inputs correct   | Yes         | warning    |

### Error Propagation

```text
G2PConfig.check()      → ConfigError (always fatal)
check_github_config()  → G2PCheckResult list
  → mode=error: G2PCheckError on any failure
  → mode=warn:  ::warning:: annotations
  → mode=skip:  no checks run
```

### G2P Exception Classes

```python
class G2PError(GerritActionError):
    """Base for G2P operations."""

class G2PConfigError(G2PError):
    """G2P configuration has problems."""

class G2PCheckError(G2PError):
    """GitHub-side check failed in error mode."""

class G2PSetupError(G2PError):
    """Failed to set up G2P inside the container."""
```

## Integration with Existing Architecture

### Where G2P Fits in the Action Flow

```text
action.yaml steps:

 1. Setup (parse inputs, set env vars)
 2. Docker BuildX setup
 3. Cache restore (optional)
 4. Detect API paths
 5. Start instances (Docker containers)
    └─ generate_replication_config()
       └─ NEW: append g2p detection remote
 6. Check services (health check)
 7. Setup Gerrit user (SSH keys, admin account)
 8. ★ NEW: Configure G2P ★
    ├─ Check GitHub config
    ├─ Generate gerrit_to_platform.ini
    ├─ Symlink replication.config
    ├─ Create hook symlinks
    ├─ Setup SSH for github.com
    └─ Output g2p results
 9. Trigger replication (if sync_on_startup)
10. Verify replication (if require_replication_success)
11. Collect outputs (includes g2p outputs)
12. Cleanup / keep-alive
```

### Integration Points

| Existing Module      | Change Needed                        |
| -------------------- | ------------------------------------ |
| `config.py`          | None; G2PConfig stands alone         |
| `docker_manager.py`  | Used by g2p_setup for `docker exec`  |
| `start-instances.py` | Add g2p remote to replication.config |
| `outputs.py`         | Add g2p outputs to collection        |
| `errors.py`          | Add G2P exception subclasses         |
| `health_check.py`    | None                                 |
| `replication.py`     | None                                 |
| `Dockerfile`         | None (g2p already present)           |

### Environment Variable Flow

```text
action.yaml inputs
  → action step env: block
    → G2P_* env vars
      → G2PConfig.from_environment()
        → g2p_setup functions
          → docker exec commands into containers
```

## Security Considerations

### Secrets Handling

| Secret                 | Handling                                   |
| ---------------------- | ------------------------------------------ |
| `g2p_github_token`     | Env var (masked by GHA); 0600 in container |
| `g2p_ssh_private_key`  | Env var (masked by GHA); 0600 in container |
| Auto-generated SSH key | Created in tmpfs; private key never output |

### File Permissions

| File                          | Owner           | Mode             |
| ----------------------------- | --------------- | ---------------- |
| `gerrit_to_platform.ini`      | `gerrit:gerrit` | `0600`           |
| `~gerrit/.ssh/g2p_github_key` | `gerrit:gerrit` | `0600`           |
| `~gerrit/.ssh/config`         | `gerrit:gerrit` | `0644`           |
| `~gerrit/.ssh/known_hosts`    | `gerrit:gerrit` | `0644`           |
| Hook symlinks                 | `gerrit:gerrit` | `0777` (symlink) |

### Token Scope — Least Privilege

Documentation and checks should recommend:

- Fine-Grained PAT (not Classic)
- Scoped to the specific organisation
- Permissions: Actions (R/W), Contents (R), Metadata (R)
- Validity: 1 year max (with calendar reminder)
- Owner: Org owner account (for automatic approval)

## File Tree of Changes

```text
gerrit-action/
├── action.yaml                  # MODIFIED
├── scripts/
│   ├── configure-g2p.py         # NEW
│   └── lib/
│       ├── errors.py            # MODIFIED
│       ├── g2p_config.py        # NEW
│       ├── g2p_github.py        # NEW
│       └── g2p_setup.py         # NEW
├── tests/
│   ├── test_g2p_config.py       # NEW
│   ├── test_g2p_github.py       # NEW
│   └── test_g2p_setup.py        # NEW
└── docs/
    └── G2P-CONFIGURATION.md     # NEW: This document
```

Files modified (minimal changes):

- `action.yaml` — New inputs, outputs, one new step
- `scripts/lib/errors.py` — Three new exception classes
- `scripts/start-instances.py` — Conditional g2p remote
- `scripts/collect-outputs.py` — Include g2p outputs

## Testing Strategy

### Unit Tests

<!-- markdownlint-disable MD013 -->

| Test File            | Scope           | Key Scenarios                         |
| -------------------- | --------------- | ------------------------------------- |
| `test_g2p_config.py` | `G2PConfig`     | Defaults, from_environment(), check() |
| `test_g2p_setup.py`  | File generation | INI, replication append, hooks, SSH   |
| `test_g2p_github.py` | API checks      | Token ok/bad, org exists/missing      |

<!-- markdownlint-enable MD013 -->

### Mock Strategy

- GitHub API: Mock `urllib.request.urlopen` with fixtures
- Docker exec: Mock `DockerManager.exec_command`
- SSH ops: Mock `subprocess.run` for keygen/keyscan
- File system: Use `tmp_path` fixtures

### Integration Scenarios

| Scenario          | Inputs                    | Expected Outcome          |
| ----------------- | ------------------------- | ------------------------- |
| G2P off (default) | `g2p_enable=false`        | No g2p work happens       |
| Minimal config    | `enable=true, owner=test` | Defaults, token warning   |
| Full config       | All inputs                | Full config, checks pass  |
| Bad token         | Invalid token             | Warning or error per mode |
| Missing owner     | `g2p_github_owner=''`     | ConfigError               |
| Custom hooks      | `hooks=patchset-created`  | One symlink               |
| Custom mappings   | Custom JSON               | Custom INI mappings       |

### Test Fixtures

```text
tests/fixtures/
├── g2p_github_workflows_response.json
├── g2p_github_org_response.json
├── g2p_github_user_response.json
├── g2p_expected_ini_default.ini
├── g2p_expected_ini_custom.ini
└── g2p_expected_replication_remote.config
```

## Future Considerations

### GitLab Support

The g2p codebase supports GitLab, but this plan covers
GitHub only. Future inputs: `g2p_gitlab_token`,
`g2p_gitlab_owner`, `g2p_gitlab_remote_url`.

### Change-Abandoned Hook

The g2p project plans a `change-abandoned` handler. When it
lands, add `change-abandoned` to `VALID_HOOKS` and support
it in hook symlink creation.

### GitHub App Authentication

A GitHub App installation token could provide better
security and higher rate limits. This would need:
`g2p_github_app_id`, `g2p_github_app_private_key`,
`g2p_github_app_installation_id`, plus JWT-based token
generation logic.

### Automatic GitHub Org Configuration

With the GraphQL API, we could offer an optional mode that
automatically configures the target GitHub org: create the
`.github` repository, scaffold required workflow files, add
deploy keys, and configure org token policies. Out of scope
for the initial implementation, but the check module's
GraphQL capabilities lay the groundwork.

### Bulk Replication Remote Support

Some organisations may need several GitHub remotes (e.g.
different repos mapped to different GitHub orgs). Future
support could accept a JSON array for `g2p_remote_config`
instead of the current single-remote approach.

## Appendix A — Example Usage

### Minimal

```yaml
- uses: lfreleng-actions/gerrit-action@main
  with:
    gerrit_setup: |
      [{"slug": "onap",
        "gerrit": "gerrit.onap.org"}]
    ssh_private_key: ${{ secrets.GERRIT_SSH_KEY }}
    g2p_enable: 'true'
    g2p_github_owner: 'onap'
    g2p_github_token: >-
      ${{ secrets.G2P_GITHUB_TOKEN }}
```

### Full

```yaml
- uses: lfreleng-actions/gerrit-action@main
  with:
    gerrit_setup: |
      [{"slug": "onap",
        "gerrit": "gerrit.onap.org",
        "api_path": "/r"}]
    ssh_private_key: ${{ secrets.GERRIT_SSH_KEY }}
    g2p_enable: 'true'
    g2p_github_owner: 'onap'
    g2p_github_token: >-
      ${{ secrets.G2P_GITHUB_TOKEN }}
    g2p_remote_name_style: 'dash'
    g2p_comment_mappings: |
      {"recheck": "verify",
       "remerge": "merge",
       "rerun-gha": "verify",
       "remerge-gha": "merge"}
    g2p_hooks: >-
      patchset-created,comment-added,change-merged
    g2p_validation_mode: 'error'
    g2p_validate_workflows: 'true'
    g2p_validate_repos: 'ci-management'
```

### Deferred GitHub Configuration

```yaml
- uses: lfreleng-actions/gerrit-action@main
  id: gerrit
  with:
    gerrit_setup: |
      [{"slug": "test",
        "gerrit": "gerrit.example.org"}]
    ssh_private_key: ${{ secrets.GERRIT_SSH_KEY }}
    g2p_enable: 'true'
    g2p_github_owner: 'my-org'
    g2p_validation_mode: 'skip'

- name: Add deploy key to GitHub
  if: >-
    steps.gerrit.outputs.g2p_ssh_public_key != ''
  run: |
    echo "SSH public key for deploy key setup:"
    echo "$G2P_PUBLIC_KEY"
  env:
    G2P_PUBLIC_KEY: >-
      ${{ steps.gerrit.outputs.g2p_ssh_public_key }}
```

## Appendix B — gerrit_to_platform.ini Reference

```ini
# Comment keyword to workflow filter mappings
[mapping "comment-added"]
recheck = verify
reverify = verify
remerge = merge
rerun-gha = verify
remerge-gha = merge

# GitHub credentials
[github.com]
token = ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# GitLab credentials (future)
# [gitlab.com]
# token = glpat-xxxxxxxxxxxxxxxxxxxx
```

## Appendix C — Replication Config G2P Section

```ini
# Appended to existing pull-replication config.
# Provides platform detection data for g2p.
# This remote does NOT replicate; no push/fetch lines.
# It exists so g2p can detect the GitHub platform, owner,
# and repository naming convention.

[remote "github-g2p"]
  url = git@github.com:onap/${name}.git
  authGroup = GitHub Replication
  remoteNameStyle = dash
```

## Appendix D — Gerrit Hook Symlinks

```text
/var/gerrit/hooks/
├── patchset-created → /opt/gerrit-tools/bin/patchset-created
├── comment-added    → /opt/gerrit-tools/bin/comment-added
└── change-merged    → /opt/gerrit-tools/bin/change-merged
```

The `hooks` plugin (bundled in the official Gerrit Docker
image) must exist for Gerrit to execute these hooks when
events occur.
