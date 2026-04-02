<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

<!-- markdownlint-disable MD013 MD060 -->

# GitHub Organisation Verification and Auto-Provisioning

> Development specification for auditing and auto-configuring
> GitHub Organisations used as `gerrit_to_platform` targets.

## Table of Contents

- [Background and Motivation](#background-and-motivation)
- [Architecture Overview](#architecture-overview)
  - [How G2P Dispatches Workflows](#how-g2p-dispatches-workflows)
  - [What the Target Org Needs](#what-the-target-org-needs)
- [Gap Analysis](#gap-analysis)
  - [Currently Checked](#currently-checked)
  - [Not Currently Checked](#not-currently-checked)
  - [Existing Infrastructure to Extend](#existing-infrastructure-to-extend)
- [Design](#design)
  - [Three-Mode Control Input](#three-mode-control-input)
  - [Required GitHub Components Checklist](#required-github-components-checklist)
  - [Multi-Org Token Secret Design](#multi-org-token-secret-design)
  - [Token Scope Requirements](#token-scope-requirements)
  - [Credential Reuse from G2P Setup](#credential-reuse-from-g2p-setup)
  - [GITHUB\_STEP\_SUMMARY Output](#github_step_summary-output)
  - [GraphQL Queries](#graphql-queries)
- [Implementation Plan](#implementation-plan)
  - [Phase 1: Audit Checks](#phase-1-audit-checks)
  - [Phase 2: Auto-Provisioning](#phase-2-auto-provisioning)
  - [Phase 3: Step Summary](#phase-3-step-summary)
  - [Phase 4: Action Wiring](#phase-4-action-wiring)
  - [Phase 5: Tests](#phase-5-tests)
  - [Phase 6: Documentation](#phase-6-documentation)
- [File Inventory](#file-inventory)
- [Future Extensions](#future-extensions)

---

## Background and Motivation

When `gerrit_to_platform` (G2P) configures itself inside a
Gerrit container deployed by `gerrit-action`, it uses
Gerrit server-side hooks to dispatch `workflow_dispatch`
events to a target GitHub Organisation. Those dispatched
workflows need organisation-level **secrets** (SSH private
keys for voting on Gerrit changes) and **variables**
(Gerrit server hostname, SSH user, known hosts, and URL)
to function.

Today, the G2P setup confirms that the target org exists,
the token works, the `.github` magic repository exists,
and that workflows with the right filenames exist. It does
**not** check whether the org has the required secrets and
variables configured. Workflows can dispatch with no errors
but then **fail at runtime** because they cannot SSH back
to the Gerrit server to vote.

This specification covers two capabilities:

1. **Audit** — Verify the target GitHub org has all the
   pieces G2P needs, and report what pieces are absent.
2. **Auto-provision** — When the audit detects absent
   pieces and the token has sufficient permissions,
   create them.

A single three-state input controls both capabilities,
letting users choose their level of automation.

---

## Architecture Overview

### How G2P Dispatches Workflows

```text
Gerrit Container (Docker)
  |-- /var/gerrit/hooks/patchset-created (symlink)
        |-- /opt/gerrit-tools/bin/patchset-created
              |-- gerrit_to_platform Python package
                    |-- Reads: gerrit_to_platform.ini
                    |          |-- [github.com] token
                    |-- Reads: replication.config
                    |          |-- [remote "github-g2p"]
                    |               url, remoteNameStyle
                    |-- Calls: GitHub API
                         POST /repos/{org}/{repo}/
                              actions/workflows/{id}/
                              dispatches
                              (workflow_dispatch event)
```

### What the Target Org Needs

The dispatched workflow runs in the target org. For it to
succeed end-to-end, the org must have:

| Layer | Component | Purpose |
|-------|-----------|---------|
| **Structural** | `.github` repository | Hosts org-wide required workflows |
| **Structural** | Workflow files matching `gerrit` + `verify`/`merge` naming | G2P discovers and dispatches these |
| **Structural** | 9 standard `GERRIT_*` `workflow_dispatch` inputs per workflow | G2P populates these on dispatch |
| **Secrets** | `GERRIT_SSH_PRIVKEY` | SSH private key for voting/commenting on Gerrit |
| **Variables** | `GERRIT_SERVER` | Gerrit SSH hostname (e.g. `gerrit.onap.org`) |
| **Variables** | `GERRIT_SSH_USER` | SSH username for Gerrit review commands |
| **Variables** | `GERRIT_KNOWN_HOSTS` | SSH known\_hosts entry for the Gerrit server |
| **Variables** | `GERRIT_URL` | Gerrit HTTP(S) URL for `checkout-gerrit-change-action` |

---

## Gap Analysis

### Currently Checked

These checks exist in `scripts/lib/g2p_github.py` today:

| Check Name | What It Tests | API |
|------------|---------------|-----|
| `token_provided` | Token string is non-empty | None |
| `token_valid` | PAT authenticates | `GET /user` |
| `org_access` | Token can see the org | `GET /orgs/{owner}` |
| `magic_repo` | `.github` repo exists | `GET /repos/{owner}/.github` |
| `workflows_{repo}_{filter}` | Workflow files match naming convention | `GET /repos/{owner}/{repo}/actions/workflows` |
| `repos_exist` | Listed repos exist | `GET /repos/{owner}/{repo}` |

### Not Currently Checked

| Gap | Severity | Impact |
|-----|----------|--------|
| **Organisation secrets** | HIGH | `GERRIT_SSH_PRIVKEY` absent means workflows fail to vote on Gerrit changes |
| **Organisation variables** | HIGH | `GERRIT_SERVER`, `GERRIT_SSH_USER`, `GERRIT_KNOWN_HOSTS`, `GERRIT_URL` absent means workflows fail at runtime |
| **Workflow `workflow_dispatch` inputs** | MEDIUM | Workflow matches filename pattern but lacks required `GERRIT_*` inputs, and dispatch returns 422 |
| **Token capability** | MEDIUM | Token authenticates but lacks `actions:write`, and dispatch returns 403 |
| **Actions enablement** | LOW | Actions may be disabled on a repo |

### Existing Infrastructure to Extend

The codebase has scaffolding designed for deeper checks
but not yet wired up:

1. **`_graphql_query()`** in `g2p_github.py` (L166-200)
   is a functional GraphQL helper that no check calls.
   The new audit checks will use it for efficient batch
   queries.

2. **`REQUIRED_WORKFLOW_INPUTS`** in `g2p_github.py`
   (L53-64) defines the 9 required input names but no
   check references them.

3. **`G2PCheckResult`** dataclass provides the result
   structure and severity system.

4. **`write_summary()`** in `scripts/lib/outputs.py`
   (L68-87) is the centralised helper for
   `$GITHUB_STEP_SUMMARY` output.

5. **`generate_ssh_keypair()`** in `g2p_setup.py`
   (L189-235) provides Ed25519 keypair generation for
   Gerrit-side SSH. The same function (or a second
   dedicated call) can serve as the source for the
   `GERRIT_SSH_PRIVKEY` org secret.

6. **`G2PSetupResult.ssh_public_key`** surfaces the
   public key from G2P SSH setup as an action output
   (`g2p_ssh_public_key`), making it available for
   downstream deploy-key registration.

---

## Design

### Three-Mode Control Input

A new action input `g2p_org_setup` controls the behaviour,
with a corresponding environment variable `G2P_ORG_SETUP`
and `G2PConfig` field `org_setup`.

| Value | Behaviour |
|-------|-----------|
| `provision` | Run all audit checks, then **create** any absent secrets, variables, or other required configuration in the target org. Requires elevated token permissions. |
| `verify` | Run all audit checks and **report** results. Absent items appear as `::warning::` annotations and in `$GITHUB_STEP_SUMMARY`. The action makes no changes to the org. This is the **default**. |
| `skip` | Do not run org-level audit checks. Use this when the org has correct configuration or when the token lacks permissions to query org settings. |

**Naming rationale**: These three values map to the
operational intent: `provision` acts, `verify` observes,
`skip` ignores. They differ from the existing
`g2p_validation_mode` input (`error`/`warn`/`skip`), which
controls the *severity* of G2P workflow/repo checks. The
new input controls a separate concern: org-level
infrastructure setup.

**`action.yaml` input definition:**

```yaml
  g2p_org_setup:
    description: |
      Controls GitHub org verification and auto-provisioning.
      Options:
        provision - Audit and auto-create absent config
        verify   - Audit and report only (default)
        skip     - Do not audit org configuration
    required: false
    default: 'verify'
```

**Environment variable mapping (in the configure-g2p step):**

```yaml
  G2P_ORG_SETUP: ${{ inputs.g2p_org_setup }}
```

**`G2PConfig` field:**

```python
org_setup: str = "verify"
```

The `G2PConfig.check()` method must enforce:

```python
if self.org_setup not in ("provision", "verify", "skip"):
    raise G2PConfigError(
        f"g2p_org_setup must be 'provision', 'verify', "
        f"or 'skip'; got '{self.org_setup}'"
    )
```

### Required GitHub Components Checklist

The audit runs these checks in dependency order:

| # | Check Name | What | API | Severity |
|---|------------|------|-----|----------|
| 1 | `org_secrets` | Org has `GERRIT_SSH_PRIVKEY` | REST or GraphQL | `error` |
| 2 | `org_variables` | Org has all 4 required variables | REST or GraphQL | `error` |
| 3 | `org_variables_populated` | Required variables hold non-empty values | REST (values visible) | `warning` |
| 4 | `workflow_inputs` | Discovered workflows have required `GERRIT_*` inputs | GraphQL (file content) or REST | `warning` |

Checks 1-3 run after the existing `org_access` check
succeeds (token works, org accessible). Check 4 runs
after the existing workflow filename checks succeed.

### Multi-Org Token Secret Design

When `gerrit-action` runs in a matrix job (one job per
Gerrit server, each targeting a different GitHub org), a
single GitHub Actions secret must map org names to their
respective tokens.

**Secret name:** `G2P_ORG_TOKENS`

**Storage format:** Base64-encoded JSON array.

Base64 encoding is **mandatory** because GitHub's secret
redaction engine applies aggressive pattern matching to
console output. When a JSON secret contains token strings,
GitHub masks any console line that partially matches, and
this causes "spurious redactions" where random parts of
log output get replaced with `***`. Base64 encoding
prevents this because the encoded form does not match the
raw token patterns that GitHub looks for.

**Inner JSON schema:**

```json
[
  {
    "github_org": "modeseven-gerrit-onap",
    "token": "ghp_xxxxxxxxxxxxxxxxxxxx"
  },
  {
    "github_org": "modeseven-gerrit-lf",
    "token": "ghp_yyyyyyyyyyyyyyyyyyyy"
  },
  {
    "github_org": "modeseven-gerrit-oran",
    "token": "github_pat_zzzzzzzzzzzzzzzz"
  }
]
```

**Field definitions:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `github_org` | string | Yes | GitHub org name, must match the `g2p_github_owner` value for this matrix entry |
| `token` | string | Yes | GitHub PAT (classic or fine-grained) with permissions to manage org secrets and variables |

**Encoding:**

```bash
# Create the JSON file
cat > org-tokens.json << 'EOF'
[
  {
    "github_org": "modeseven-gerrit-onap",
    "token": "ghp_xxxxxxxxxxxxxxxxxxxx"
  },
  {
    "github_org": "modeseven-gerrit-lf",
    "token": "ghp_yyyyyyyyyyyyyyyyyyyy"
  }
]
EOF

# Base64-encode for GitHub secret storage
base64 < org-tokens.json
# Copy the output and set as the G2P_ORG_TOKENS secret
```

**Decoding in the action (Python):**

```python
import base64
import json

def decode_org_tokens(
    b64_value: str,
) -> dict[str, str]:
    """Decode the G2P_ORG_TOKENS secret.

    Returns a dict mapping github_org to token.
    """
    decoded = base64.b64decode(b64_value).decode("utf-8")
    entries = json.loads(decoded)
    return {
        entry["github_org"]: entry["token"]
        for entry in entries
    }
```

**Design rationale:**

- **Extensible** -- append new orgs to the JSON array
  without code changes.
- **Matrix-compatible** -- each matrix job looks up its
  token by `g2p_github_owner`, letting different orgs
  use different tokens with different permission levels.
- **Consistent** -- follows the base64-encoded JSON
  pattern from `GERRIT_CREDENTIALS` in
  `test-deploy-gerrit`, with the same decode pipeline:
  base64 decode, JSON parse, lookup by key.

**Action input for the secret:**

```yaml
  g2p_org_token_map:
    description: |
      Base64-encoded JSON array mapping GitHub org names
      to PATs for org-level provisioning. Required when
      g2p_org_setup is 'provision'. Each entry needs:
      {"github_org": "org-name", "token": "ghp_xxx"}
      See docs/GITHUB-ORG-VERIFY-CONFIG.md for details.
    required: false
    default: ''
```

**Token lookup flow:**

```text
action input: g2p_org_token_map (base64 string)
  -> env var: G2P_ORG_TOKEN_MAP
    -> Python: base64.b64decode then json.loads
      -> dict[github_org] -> token
        -> Used for: org secrets/variables API calls
```

When `g2p_org_setup` is `verify`, the existing
`g2p_github_token` suffices (it needs read access to
list secrets/variables). When `g2p_org_setup` is
`provision`, the `g2p_org_token_map` provides the
elevated-permission token for write operations.

If `g2p_org_token_map` has no value and `g2p_org_setup`
is `provision`, the code falls back to
`g2p_github_token` for both read and write operations.
This supports the simple case where a single token has
all required permissions.

### Token Scope Requirements

#### For `verify` Mode (Read-Only Audit)

The existing `g2p_github_token` suffices.

| Token Type | Required Scopes |
|------------|----------------|
| **Classic PAT** | `read:org` (to list org secrets and variables) |
| **Fine-grained PAT** | Organization: Secrets (read), Variables (read), Administration (read) |

#### For `provision` Mode (Read + Write)

The `g2p_org_token_map` token (or fallback
`g2p_github_token`) needs:

| Token Type | Required Scopes |
|------------|----------------|
| **Classic PAT** | `admin:org`, `repo` (for org secrets/variables write, repo administration) |
| **Fine-grained PAT** | Organization: Secrets (read/write), Variables (read/write), Administration (read/write) |

**Recommended classic PAT scopes for full provisioning:**

- `admin:org` -- manage org secrets and variables
- `repo` -- required for fine-grained secret visibility
  scoping (secrets can target specific repos)
- `workflow` -- may prove necessary if provisioning
  includes workflow file operations in future

> **Note:** Classic tokens are simpler to configure for
> cross-org use since fine-grained tokens target a single
> org. For multi-org matrix jobs, classic tokens are the
> practical choice today. Each org should still have its
> own dedicated token for least-privilege.

### Credential Reuse from G2P Setup

The G2P setup phase (`g2p_setup.py`) generates or deploys
an Ed25519 SSH keypair for Gerrit-side replication. The
design reuses credentials from this phase rather than
generating new ones:

**SSH keypair for `GERRIT_SSH_PRIVKEY`:**

The G2P setup generates (or accepts) an SSH keypair stored
at `/var/gerrit/.ssh/g2p_github_key` inside the container.
The public key appears as the `g2p_ssh_public_key` action
output.

For the `GERRIT_SSH_PRIVKEY` org secret, the workflow
needs an SSH key that authenticates to the Gerrit server
for voting. This is a **different keypair** -- it
authenticates *to Gerrit*, not to GitHub. However:

1. The Gerrit container receives SSH keys via the
   `ssh_auth_keys` input (public keys get added to the
   Gerrit user's account).
2. The corresponding private key must be the
   `GERRIT_SSH_PRIVKEY` org secret.
3. In the CI test environment, both sides are under our
   control (the Gerrit container and the GitHub org),
   which allows generating a dedicated keypair during
   setup and deploying both halves.

**Proposed flow for `provision` mode:**

```text
1. Generate Ed25519 keypair (gerrit-review-key)
   |-- Private key -> GERRIT_SSH_PRIVKEY org secret
   |-- Public key  -> Gerrit user SSH keys
                     (via setup-gerrit-user.py)

2. Gerrit container config (done during setup):
   |-- SSH host key -> known_hosts format
   |   |-- GERRIT_KNOWN_HOSTS org variable
   |-- SSH port + hostname -> GERRIT_SERVER variable
   |-- SSH username -> GERRIT_SSH_USER variable
   |-- HTTP URL -> GERRIT_URL variable
```

The Gerrit container's SSH host key, port, username, and
HTTP URL all come from the `instances.json` file written
by `gerrit-action` during deployment. All four org
variables derive from this data.

**Implementation detail -- instances.json fields:**

| Variable | Source in instances.json |
|----------|------------------------|
| `GERRIT_SERVER` | Constructed from `ssh_host` + `ssh_port` (e.g. `localhost` or tunnel hostname) |
| `GERRIT_SSH_USER` | From `ssh_auth_username` input |
| `GERRIT_KNOWN_HOSTS` | Generated via `ssh-keyscan -p {port} {host}` against the running container |
| `GERRIT_URL` | From `http_url` field (e.g. `http://localhost:8080/r/`) |

### GITHUB\_STEP\_SUMMARY Output

All G2P check results (existing and new) render in the
step summary for easy visibility. This uses the
established `write_summary()` helper from
`scripts/lib/outputs.py`.

**Summary section format:**

```markdown
## G2P Organisation Audit: `modeseven-gerrit-onap`

| Check | Status | Details |
|-------|--------|---------|
| Token provided | PASS | Token ending in `...abc` |
| Token works | PASS | Authenticated as `bot-user` |
| Org accessible | PASS | Organisation found |
| `.github` repo | PASS | Repository exists |
| Org secrets | MISSING | `GERRIT_SSH_PRIVKEY` |
| Org variables | MISSING | `GERRIT_SERVER`, `GERRIT_URL` |
| Workflows (verify) | PASS | `gerrit-required-verify.yaml` |
| Workflows (merge) | PASS | `gerrit-merge.yaml` |

**Mode:** `verify` -- reporting only, no changes made.

### Absent Items

- **Secret `GERRIT_SSH_PRIVKEY`** -- SSH private key for
  Gerrit review voting. Set `g2p_org_setup: provision`
  to auto-create.
- **Variable `GERRIT_SERVER`** -- Gerrit SSH hostname.
- **Variable `GERRIT_URL`** -- Gerrit HTTP URL.
```

When `g2p_org_setup` is `provision`, the summary changes:

```markdown
**Mode:** `provision` -- auto-provisioning enabled.

### Provisioned Items

- Created secret `GERRIT_SSH_PRIVKEY`
- Created variable `GERRIT_SERVER` = `localhost:29418`
- Created variable `GERRIT_URL` =
  `http://localhost:8080/r/`
- Variable `GERRIT_SSH_USER` already exists (skipped)
- Variable `GERRIT_KNOWN_HOSTS` already exists (skipped)
```

**Implementation -- new function in `g2p_github.py`:**

```python
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
        The g2p_org_setup mode value.
    provisioned:
        Descriptions of items auto-provisioned
        (used when mode is 'provision').

    Returns
    -------
    str
        Markdown content for $GITHUB_STEP_SUMMARY.
    """
```

This function returns a Markdown string; the caller
passes it to `write_summary()`. This separation keeps
`g2p_github.py` independent of the I/O mechanism and
makes the summary content testable.

**Future extensibility:** The `write_summary()` helper
and the summary-rendering function can serve other parts
of `gerrit-action` (e.g. container setup, replication
verification) following the same pattern.

### GraphQL Queries

The existing `_graphql_query()` helper in `g2p_github.py`
handles efficient batch queries. REST API fallbacks cover
cases where GraphQL does not apply (e.g. secret encryption
key retrieval).

#### Query 1: Organisation Secrets Audit

```graphql
query OrgSecretsAudit($org: String!) {
  organization(login: $org) {
    login
    secrets: organizationSecrets(first: 100) {
      nodes {
        name
        createdAt
        updatedAt
      }
      totalCount
    }
  }
}
```

> **Note:** If the GraphQL org secrets endpoint proves
> unavailable or returns permission errors, fall back to
> the REST endpoint `GET /orgs/{org}/actions/secrets`.

**REST fallback:**

```text
GET /orgs/{org}/actions/secrets
Authorization: Bearer {token}

Response: {
  "total_count": 2,
  "secrets": [
    {"name": "GERRIT_SSH_PRIVKEY", "created_at": "...", ...},
    ...
  ]
}
```

The REST API returns secret **names** only (never values).
This suffices for presence checks.

#### Query 2: Organisation Variables Audit

```text
GET /orgs/{org}/actions/variables
Authorization: Bearer {token}

Response: {
  "total_count": 4,
  "variables": [
    {"name": "GERRIT_SERVER", "value": "gerrit.onap.org", ...},
    ...
  ]
}
```

The REST API returns variable **names and values**. This
enables checking both presence and non-emptiness.

> **Note:** At time of writing, GitHub's GraphQL schema
> does not expose Actions variables. Use REST for this
> check.

#### Query 3: Workflow Content (Input Checking)

To verify that workflows have the required
`workflow_dispatch` inputs, fetch the file content:

```graphql
query WorkflowContent(
  $owner: String!
  $repo: String!
  $path: String!
) {
  repository(owner: $owner, name: $repo) {
    object(expression: "HEAD:{path}") {
      ... on Blob {
        text
      }
    }
  }
}
```

Variables: `path` = `.github/workflows/gerrit-verify.yaml`

The returned YAML gets parsed locally to check for the
required `workflow_dispatch` inputs defined in the
`REQUIRED_WORKFLOW_INPUTS` constant.

#### Secret Encryption (for Provisioning)

GitHub requires encrypting secrets with the org's public
key before upload. The flow:

```text
1. GET /orgs/{org}/actions/secrets/public-key
   -> { "key_id": "...", "key": "<base64 public key>" }

2. Encrypt secret value using libsodium sealed box:
   - Decode the base64 public key
   - Use PyNaCl (or nacl bindings) to create a
     SealedBox and encrypt the secret value
   - Base64-encode the encrypted bytes

3. PUT /orgs/{org}/actions/secrets/{name}
   Body: {
     "encrypted_value": "<base64 encrypted>",
     "key_id": "<from step 1>",
     "visibility": "all"
   }
```

**Dependency:** `PyNaCl` handles the libsodium sealed-box
encryption. This must be available in the CI runner's
Python environment (not in the Docker container's venv)
since the provisioning code runs on the **GitHub Actions
runner** as part of `configure-g2p.py`.

The `pip install PyNaCl` can run as a setup step, or the
code can fall back to `subprocess` + `openssl` if PyNaCl
proves unavailable.

**Alternative -- `subprocess` with `openssl`:**

As a fallback, the encryption can use `openssl` if
available on the runner:

```python
import subprocess

def encrypt_secret_openssl(
    public_key_b64: str,
    secret_value: str,
) -> str:
    """Encrypt using openssl (fallback).

    This requires a specific openssl build with
    X25519 support, which is not universal.
    PyNaCl is the preferred method.
    """
    ...
```

The recommendation: use PyNaCl as the primary method with
clear error messaging if it proves unavailable.

### Variable Provisioning

Variables are simpler since they use plaintext storage:

```text
POST /orgs/{org}/actions/variables
Authorization: Bearer {token}
Body: {
  "name": "GERRIT_SERVER",
  "value": "localhost:29418",
  "visibility": "all"
}
```

For updating existing variables:

```text
PATCH /orgs/{org}/actions/variables/{name}
Authorization: Bearer {token}
Body: {
  "name": "GERRIT_SERVER",
  "value": "localhost:29418",
  "visibility": "all"
}
```

The provisioning code uses PATCH for existing variables
and POST for new ones. It checks existence first via the
audit query, then acts accordingly.

---

## Implementation Plan

### Phase 1: Audit Checks

**Files to modify:** `scripts/lib/g2p_github.py`

#### 1a. Add `check_org_secrets()`

```python
# Required secrets at the org level
REQUIRED_ORG_SECRETS: tuple[str, ...] = (
    "GERRIT_SSH_PRIVKEY",
)

# Optional secrets (warn if absent, do not error)
OPTIONAL_ORG_SECRETS: tuple[str, ...] = (
    "GERRIT_SSH_PRIVKEY_G2G",
)


def check_org_secrets(
    token: str,
    owner: str,
) -> G2PCheckResult:
    """Check the org has required Actions secrets.

    Uses REST: GET /orgs/{owner}/actions/secrets
    Falls back gracefully on 403 (insufficient perms).
    """
```

**Behaviour:**

- List all org-level secret names
- Compare against `REQUIRED_ORG_SECRETS`
- Return `passed=True` when all required names exist
- `severity="error"` for absent required secrets
- `severity="warning"` for absent optional secrets
- Include `details={"missing": [...], "found": [...]}`
- On 403 (token lacks permissions), return
  `severity="warning"` with a clear message explaining
  the token needs `read:org` or equivalent

#### 1b. Add `check_org_variables()`

```python
REQUIRED_ORG_VARIABLES: tuple[str, ...] = (
    "GERRIT_SERVER",
    "GERRIT_SSH_USER",
    "GERRIT_KNOWN_HOSTS",
    "GERRIT_URL",
)


def check_org_variables(
    token: str,
    owner: str,
) -> G2PCheckResult:
    """Check the org has required Actions variables.

    Uses REST: GET /orgs/{owner}/actions/variables
    Also checks that variable values are non-empty.
    """
```

**Behaviour:**

- List all org-level variables (names + values)
- Check required names exist
- Check values hold non-empty strings
- Return `passed=True` only when all exist and hold data
- `severity="error"` for absent variables
- `severity="warning"` for empty values
- Include `details={"missing": [...], "empty": [...],
  "found": [...]}`

#### 1c. Activate `REQUIRED_WORKFLOW_INPUTS` Check

The constant already exists. Add a new function:

```python
def check_workflow_inputs(
    token: str,
    owner: str,
    repo: str,
    workflow_path: str,
) -> G2PCheckResult:
    """Verify a workflow file has required inputs.

    Uses GraphQL to fetch file content, then parses
    the YAML to check for GERRIT_* inputs under
    on.workflow_dispatch.inputs.
    """
```

**Behaviour:**

- Fetch workflow file content via GraphQL
- Parse YAML (using `yaml.safe_load` -- `PyYAML` ships
  with GitHub runners)
- Check `on.workflow_dispatch.inputs` has all 9
  `REQUIRED_WORKFLOW_INPUTS`
- `severity="warning"` for absent inputs (the workflow
  might still work for a subset of events)

#### 1d. Wire into `check_github_config()`

Add the new checks after the existing dependency chain:

```python
def check_github_config(config: G2PConfig) -> list:
    # ... existing checks 1-7 ...

    # -- Check 8: Org secrets (NEW) -----------------
    if config.org_setup != "skip":
        results.append(
            check_org_secrets(
                config.github_token, config.github_owner
            )
        )

    # -- Check 9: Org variables (NEW) ---------------
    if config.org_setup != "skip":
        results.append(
            check_org_variables(
                config.github_token, config.github_owner
            )
        )

    # -- Check 10: Workflow inputs (NEW) -------------
    # Run for each discovered workflow from checks 5-6
    # (requires extracting workflow paths from earlier
    #  check results)
```

### Phase 2: Auto-Provisioning

**Files to modify:** `scripts/lib/g2p_github.py` (new
functions), `scripts/configure-g2p.py` (orchestration)

#### 2a. Add provisioning functions

```python
def provision_org_secret(
    token: str,
    owner: str,
    secret_name: str,
    secret_value: str,
) -> G2PCheckResult:
    """Create or update an org-level Actions secret.

    1. Fetch org public key
    2. Encrypt the value with PyNaCl
    3. PUT the encrypted secret
    """


def provision_org_variable(
    token: str,
    owner: str,
    variable_name: str,
    variable_value: str,
) -> G2PCheckResult:
    """Create or update an org-level Actions variable.

    Uses POST for new variables, PATCH for existing.
    """
```

#### 2b. Add the provisioning orchestrator

```python
def provision_org_config(
    config: G2PConfig,
    audit_results: list[G2PCheckResult],
    gerrit_info: dict[str, str],
    org_token: str | None = None,
) -> list[G2PCheckResult]:
    """Auto-provision absent org configuration.

    Parameters
    ----------
    config:
        G2P configuration.
    audit_results:
        Results from the audit phase, used to
        determine what is absent.
    gerrit_info:
        Dict with keys: 'ssh_host', 'ssh_port',
        'ssh_user', 'http_url', 'known_hosts',
        'ssh_private_key'.
    org_token:
        Elevated-permission token for org write
        ops. Falls back to config.github_token.

    Returns
    -------
    list[G2PCheckResult]
        Results of provisioning operations.
    """
```

**Provisioning logic:**

1. Inspect `audit_results` for failed `org_secrets` and
   `org_variables` checks
2. For each absent secret:
   - Generate or retrieve the value (e.g. SSH key from
     `gerrit_info["ssh_private_key"]`)
   - Call `provision_org_secret()`
3. For each absent variable:
   - Derive the value from `gerrit_info`
   - Call `provision_org_variable()`
4. Return results for each provisioning action

#### 2c. SSH keypair for GERRIT\_SSH\_PRIVKEY

When provisioning the `GERRIT_SSH_PRIVKEY` secret:

1. Generate a new Ed25519 keypair (reuse
   `generate_ssh_keypair()` from `g2p_setup.py`)
2. Deploy the **private key** as the org secret
3. Deploy the **public key** to the Gerrit container
   user's SSH authorised keys (via `setup-gerrit-user.py`
   or by adding to the `ssh_auth_keys` input)
4. Emit the public key in the step summary for manual
   registration if needed

**Important:** This is a **separate keypair** from the
G2P replication key. The G2P replication key authenticates
the Gerrit server to GitHub for push operations. The
`GERRIT_SSH_PRIVKEY` authenticates the GitHub Actions
workflow runner to the Gerrit server for review voting.

#### 2d. Token map decoding

Add to `G2PConfig` or `configure-g2p.py`:

```python
def resolve_org_token(
    config: G2PConfig,
) -> str:
    """Resolve the token for org-level operations.

    Priority:
    1. g2p_org_token_map entry for this github_owner
    2. g2p_github_token (fallback)
    """
    if config.org_token_map:
        tokens = decode_org_tokens(config.org_token_map)
        token = tokens.get(config.github_owner)
        if token:
            return token
        logger.warning(
            "No entry for '%s' in g2p_org_token_map; "
            "falling back to g2p_github_token",
            config.github_owner,
        )
    return config.github_token
```

### Phase 3: Step Summary

**Files to modify:** `scripts/lib/g2p_github.py` (new
function), `scripts/configure-g2p.py` (call site)

#### 3a. Add summary rendering function

```python
def format_check_results_summary(
    results: list[G2PCheckResult],
    owner: str,
    mode: str,
    provisioned: list[str] | None = None,
) -> str:
    """Render audit results as Markdown for summary."""
```

(See the format specification in the GITHUB\_STEP\_SUMMARY
Output section above.)

#### 3b. Wire into `configure-g2p.py`

After the audit and optional provisioning phases:

```python
from outputs import write_summary

# Render and write summary
summary_md = format_check_results_summary(
    results=all_results,
    owner=config.github_owner,
    mode=config.org_setup,
    provisioned=provisioned_items,
)
write_summary(summary_md)
```

### Phase 4: Action Wiring

**Files to modify:** `action.yaml`

#### 4a. New inputs

```yaml
  g2p_org_setup:
    description: |
      Controls GitHub org verification/provisioning.
      Options: provision, verify, skip
    required: false
    default: 'verify'
  g2p_org_token_map:
    description: |
      Base64-encoded JSON mapping org names to PATs
      for provisioning. See docs for format.
    required: false
    default: ''
```

#### 4b. New environment variable mappings

Add to the configure-g2p step's `env:` block:

```yaml
  G2P_ORG_SETUP: ${{ inputs.g2p_org_setup }}
  G2P_ORG_TOKEN_MAP: ${{ inputs.g2p_org_token_map }}
```

#### 4c. New outputs

```yaml
  g2p_org_audit_results:
    description: >-
      JSON array of org-level audit check results.
    value: >-
      ${{ steps.configure-g2p.outputs.g2p_org_audit_results
         || steps.g2p-defaults.outputs.g2p_org_audit_results }}
  g2p_org_provisioned:
    description: >-
      Whether any org items were auto-provisioned (created/updated).
    value: >-
      ${{ steps.configure-g2p.outputs.g2p_org_provisioned
         || steps.g2p-defaults.outputs.g2p_org_provisioned }}
```

And in the g2p-defaults step:

```yaml
  echo "g2p_org_audit_results=[]" >> "$GITHUB_OUTPUT"
  echo "g2p_org_provisioned=false" >> "$GITHUB_OUTPUT"
```

### Phase 5: Tests

**Files to create/modify:**

| File | Changes |
|------|---------|
| `tests/test_g2p_github.py` | New test classes for org audit checks |
| `tests/test_g2p_integration.py` | Integration tests for audit + provision flow |
| `tests/fixtures/g2p_github_org_secrets_response.json` | Mock org secrets API response |
| `tests/fixtures/g2p_github_org_variables_response.json` | Mock org variables API response |
| `tests/fixtures/g2p_github_workflow_content_response.json` | Mock workflow file content |

#### Test Classes to Add

**In `test_g2p_github.py`:**

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestCheckOrgSecrets` | 6+ | All present, some absent, none present, API error, permission denied, empty response |
| `TestCheckOrgVariables` | 7+ | All present and populated, some absent, empty values, API error, permission denied, partial |
| `TestCheckWorkflowInputs` | 5+ | All inputs present, some absent, no workflow\_dispatch, malformed YAML, GraphQL error |
| `TestProvisionOrgSecret` | 5+ | Success, encryption error, API error, update existing, permission denied |
| `TestProvisionOrgVariable` | 5+ | Create new, update existing, API error, permission denied, empty value |
| `TestProvisionOrgConfig` | 4+ | Full provision, partial provision, nothing absent, provision error handling |
| `TestFormatCheckResultsSummary` | 4+ | All pass, some fail, provision mode, verify mode |
| `TestDecodeOrgTokens` | 5+ | Correct input, bad base64, bad JSON, absent fields, empty array |
| `TestResolveOrgToken` | 4+ | Found in map, not found (fallback), no map (fallback), empty map |

**In `test_g2p_integration.py`:**

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestOrgAuditVerifyMode` | 3+ | Audit passes, audit warns, audit skipped |
| `TestOrgAuditProvisionMode` | 3+ | Provision succeeds, provision partial, provision fails |
| `TestOrgSetupConfigParsing` | 3+ | Correct values, incorrect value, default |

#### Mock Fixtures to Add

**`g2p_github_org_secrets_response.json`:**

```json
{
  "total_count": 2,
  "secrets": [
    {
      "name": "GERRIT_SSH_PRIVKEY",
      "created_at": "2025-01-15T00:00:00Z",
      "updated_at": "2025-01-15T00:00:00Z",
      "visibility": "all"
    },
    {
      "name": "SOME_OTHER_SECRET",
      "created_at": "2025-01-15T00:00:00Z",
      "updated_at": "2025-01-15T00:00:00Z",
      "visibility": "all"
    }
  ]
}
```

**`g2p_github_org_variables_response.json`:**

```json
{
  "total_count": 4,
  "variables": [
    {
      "name": "GERRIT_SERVER",
      "value": "gerrit.onap.org",
      "created_at": "2025-01-15T00:00:00Z",
      "updated_at": "2025-01-15T00:00:00Z",
      "visibility": "all"
    },
    {
      "name": "GERRIT_SSH_USER",
      "value": "lfci",
      "created_at": "2025-01-15T00:00:00Z",
      "updated_at": "2025-01-15T00:00:00Z",
      "visibility": "all"
    },
    {
      "name": "GERRIT_KNOWN_HOSTS",
      "value": "gerrit.onap.org ssh-ed25519 AAAA...",
      "created_at": "2025-01-15T00:00:00Z",
      "updated_at": "2025-01-15T00:00:00Z",
      "visibility": "all"
    },
    {
      "name": "GERRIT_URL",
      "value": "https://gerrit.onap.org/r/",
      "created_at": "2025-01-15T00:00:00Z",
      "updated_at": "2025-01-15T00:00:00Z",
      "visibility": "all"
    }
  ]
}
```

#### Mocking Pattern

Follow the established pattern using
`@patch("g2p_github.urlopen")` with
`_make_urlopen_response()` and `_make_http_error()`
helpers. For provisioning tests, the mock side\_effect
chain grows longer (audit calls + provisioning calls).

For `PyNaCl` encryption in provisioning tests, mock the
`nacl.public.SealedBox.encrypt` call to return predictable
bytes.

### Phase 6: Documentation

**Files to create/modify:**

| File | Changes |
|------|---------|
| `docs/GITHUB-ORG-VERIFY-CONFIG.md` | This document (reference specification) |
| `README.md` | Add org setup section to G2P documentation |
| `action.yaml` | Input/output descriptions (covered in Phase 4) |

**README.md additions:**

Add a new subsection under the existing G2P section:

- **Organisation Setup** -- describes the three modes
- **Token Requirements** -- table of scopes per mode
- **Multi-Org Tokens** -- how to configure
  `G2P_ORG_TOKENS` for matrix jobs
- **Auto-Provisioning** -- what the action creates and when

---

## File Inventory

Complete list of files this feature affects:

| File | Action | Phase |
|------|--------|-------|
| `scripts/lib/g2p_github.py` | Modify -- add audit + provision functions, summary renderer | 1, 2, 3 |
| `scripts/lib/g2p_config.py` | Modify -- add `org_setup` and `org_token_map` fields | 1 |
| `scripts/configure-g2p.py` | Modify -- wire audit/provision/summary into orchestration | 1, 2, 3 |
| `action.yaml` | Modify -- add inputs, outputs, env mappings | 4 |
| `tests/test_g2p_github.py` | Modify -- add test classes | 5 |
| `tests/test_g2p_integration.py` | Modify -- add integration tests | 5 |
| `tests/test_g2p_config.py` | Modify -- add org\_setup/token\_map parsing tests | 5 |
| `tests/fixtures/g2p_github_org_secrets_response.json` | Create | 5 |
| `tests/fixtures/g2p_github_org_variables_response.json` | Create | 5 |
| `tests/fixtures/g2p_github_workflow_content_response.json` | Create | 5 |
| `README.md` | Modify -- add org setup documentation | 6 |
| `docs/GITHUB-ORG-VERIFY-CONFIG.md` | Create -- this specification | 6 |
| `docker/requirements.txt` | May need `PyNaCl` if encryption runs in container | 2 |

---

## Future Extensions

This design accommodates future needs:

### Near-Term

- **Per-repo secret/variable overrides** -- the audit
  functions accept `owner` and could gain a `repo`
  parameter for repo-level overrides.
- **Deploy key management** -- add
  `check_deploy_keys()` and `provision_deploy_key()`
  using the same pattern.
- **Workflow template deployment** -- while not in scope
  for this feature, the provisioning framework could
  later deploy workflow templates from a library.

### Medium-Term

- **Rate limit awareness** -- check `GET /rate_limit`
  before running batch operations and back off when
  approaching limits.
- **Dry-run mode** -- a fourth mode value (`dry-run`)
  that shows what *would* change without making changes.
- **Diff mode** -- compare current org configuration
  against desired state and show a diff.

### Long-Term

- **GitLab support** -- the same audit/provision pattern
  can extend to GitLab target orgs when
  `gerrit_to_platform` adds GitLab support. The
  `G2PCheckResult` dataclass and `write_summary()` helper
  are platform-agnostic.
- **GitHub App authentication** -- replace PATs with
  GitHub App installation tokens for better security and
  permission management.
- **Configuration-as-code** -- define desired org
  configuration in a YAML file and reconcile against
  the live state.

---

## Appendix A: Example `test-deploy-gerrit` Configuration

### Workflow Input Updates

The `test-gerrit-deploy.yaml` workflow needs these
additions to pass through the new inputs:

```yaml
    # In the workflow_dispatch inputs section:
    g2p_org_setup:
      description: >-
        G2P org setup mode: provision, verify, skip
      type: choice
      options:
        - verify
        - provision
        - skip
      default: 'verify'

    # In the deploy-gerrit job, gerrit-action step:
    g2p_org_setup: ${{ inputs.g2p_org_setup }}
    g2p_org_token_map: >-
      ${{
        inputs.g2p_org_setup == 'provision' &&
          secrets.G2P_ORG_TOKENS || ''
      }}
```

### `G2P_ORG_TOKENS` Secret Value

This is the **inner JSON** before base64 encoding.
Create a file `g2p-org-tokens.json`:

```json
[
  {
    "github_org": "modeseven-gerrit-onap",
    "token": "ghp_YOUR_ONAP_ORG_TOKEN_HERE"
  },
  {
    "github_org": "modeseven-gerrit-lf",
    "token": "ghp_YOUR_LF_ORG_TOKEN_HERE"
  },
  {
    "github_org": "modeseven-gerrit-oran",
    "token": "ghp_YOUR_ORAN_ORG_TOKEN_HERE"
  },
  {
    "github_org": "modeseven-gerrit-opendaylight",
    "token": "ghp_YOUR_ODL_ORG_TOKEN_HERE"
  }
]
```

Encode and store:

```bash
base64 < g2p-org-tokens.json | pbcopy
# Paste into GitHub Settings Secrets G2P_ORG_TOKENS
# Delete the plaintext file immediately
rm g2p-org-tokens.json
```

For initial testing with a single token across all orgs,
use the same token in every entry. Later, create per-org
tokens with least-privilege scopes.

### Classic Token Scopes for Provisioning

Create a classic PAT at
`https://github.com/settings/tokens/new` with:

- [x] `admin:org` -- full control of orgs and teams
- [x] `repo` -- full control of private repositories
- [x] `workflow` -- update GitHub Action workflows

This token goes in the `G2P_ORG_TOKENS` secret entries.
A single token can serve multiple orgs if the token owner
is an admin/owner of all target orgs.

---

## Appendix B: API Reference

### REST Endpoints Used

| Endpoint | Method | Purpose | Auth Required |
|----------|--------|---------|---------------|
| `GET /orgs/{org}/actions/secrets` | GET | List org secret names | `admin:org` or Org Secrets (read) |
| `GET /orgs/{org}/actions/secrets/public-key` | GET | Get encryption key for secret upload | `admin:org` or Org Secrets (read) |
| `PUT /orgs/{org}/actions/secrets/{name}` | PUT | Create/update org secret | `admin:org` or Org Secrets (write) |
| `GET /orgs/{org}/actions/variables` | GET | List org variables (names + values) | `admin:org` or Org Variables (read) |
| `POST /orgs/{org}/actions/variables` | POST | Create org variable | `admin:org` or Org Variables (write) |
| `PATCH /orgs/{org}/actions/variables/{name}` | PATCH | Update org variable | `admin:org` or Org Variables (write) |

### GraphQL Queries Used

| Query | Purpose | Fallback |
|-------|---------|----------|
| `organization.secrets` | Batch-fetch org secret names | REST `GET /orgs/{org}/actions/secrets` |
| `repository.object(expression)` | Fetch workflow file content for input checking | REST `GET /repos/{owner}/{repo}/contents/{path}` |

---

## Appendix C: Error Handling Matrix

| Scenario | Behaviour |
|----------|-----------|
| Token lacks `admin:org` in `verify` mode | Warning: "Cannot audit org secrets/variables -- insufficient permissions" |
| Token lacks `admin:org` in `provision` mode | Error: "Cannot provision org config -- token needs admin:org scope" |
| `g2p_org_token_map` provided but no entry for this org | Warning: falls back to `g2p_github_token` |
| `g2p_org_token_map` holds bad base64 | Warning: falls back to `g2p_github_token` with message "Failed to decode g2p_org_token_map" |
| `g2p_org_token_map` holds correct base64 but bad JSON | Warning: falls back to `g2p_github_token` with message "Failed to parse g2p_org_token_map" |
| PyNaCl not available for secret encryption | Error: "PyNaCl is required for secret provisioning -- install with: pip install PyNaCl" |
| Secret encryption fails | Error with details; secret not created |
| Variable creation returns 409 (already exists) | Switch to PATCH (update) |
| Rate limit exceeded | Warning: "GitHub API rate limit reached -- retry later" |
| Network error during provisioning | Error: individual item fails, others continue |
| `.github` repo does not exist | Warning: "The .github magic repository does not exist in {org} -- org-wide required workflows cannot run" |
