<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

<!-- markdownlint-disable MD013 MD060 -->

# `gerrit_to_platform` Logging and Diagnostics Improvements

> Development brief for retrofitting comprehensive, production-grade
> logging into the upstream `gerrit_to_platform` Python package so
> that operators can diagnose its behaviour inside ephemeral
> container environments without reading source or attaching a
> debugger.

## Table of Contents

- [Background](#background)
- [Problem Statement](#problem-statement)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Current State Analysis](#current-state-analysis)
- [Design Principles](#design-principles)
- [Proposed Design](#proposed-design)
  - [1. Adopt the Standard `logging` Module](#1-adopt-the-standard-logging-module)
  - [2. Per-Module Loggers](#2-per-module-loggers)
  - [3. Configuration Surface](#3-configuration-surface)
  - [4. Log Format and Structure](#4-log-format-and-structure)
  - [5. Correlation IDs](#5-correlation-ids)
  - [6. Sinks](#6-sinks)
  - [7. Sensitive Data Handling](#7-sensitive-data-handling)
  - [8. Hook Lifecycle Instrumentation](#8-hook-lifecycle-instrumentation)
  - [9. GitHub API Instrumentation](#9-github-api-instrumentation)
  - [10. Backwards Compatibility](#10-backwards-compatibility)
- [Implementation Plan](#implementation-plan)
- [Testing Strategy](#testing-strategy)
- [Migration and Rollout](#migration-and-rollout)
- [Operational Playbook](#operational-playbook)
- [Open Questions](#open-questions)
- [References](#references)

---

## Background

`gerrit_to_platform` (G2P) is a Python package installed inside
the Gerrit container and invoked via Gerrit hook scripts
(`patchset-created`, `comment-added`, `change-merged`). Each
invocation:

1. Parses a Gerrit hook event (passed as command-line arguments).
2. Reads `gerrit_to_platform.ini` for the GitHub token, comment
   keyword mappings, and detection settings.
3. Reads `replication.config` to resolve the target platform
   (GitHub vs GitLab) and repository name.
4. Calls the GitHub REST API to dispatch matching workflows
   (`POST /repos/{owner}/{repo}/actions/workflows/{id}/dispatches`).

The `gerrit-action` repository wraps this lifecycle for ephemeral
CI deployments. See `docs/G2P-CONFIGURATION.md` for the
in-container configuration model and
`docs/GITHUB-ORG-VERIFY-CONFIG.md` for the org-side audit and
provisioning flow.

## Problem Statement

The current `gerrit_to_platform` package operates as a black
box once installed. After a Gerrit change goes through upload,
review, or merge, the operator has no way to determine:

- Whether the hook script ran at all.
- What event payload it received.
- Which GitHub repository it resolved the Gerrit project to.
- Whether it found a matching workflow.
- Whether the GitHub API call succeeded, returned a non-2xx,
  or never fired.
- Why a dispatch failed without surfacing any error.

The single signal available today is "comments appear on the
change" or they do not. When dispatch fails, the failure mode
remains indistinguishable between configuration drift, missing
target repos, missing workflows, expired tokens, network
failures, and bugs in the dispatcher itself.

This suffices for the long-running production deployment at
`gerrit.linuxfoundation.org` where the configuration stays
stable and operators have shell access. It does **not** suffice
for the new ephemeral container deployment model used by
`gerrit-action` and `test-deploy-gerrit`, where the container
exists during a single workflow run and the post-mortem
artefact reduces to the GitHub Actions log.

The `gerrit-action` repository ships a hook wrapper script
(`/var/gerrit/hooks/<hook>` that tees to
`/var/gerrit/logs/g2p-hooks.log`) as a tactical workaround that
records hook entry, argv, exit code, and combined stdout/stderr.
That wrapper proves the hook fired and what the script printed,
but it cannot reach inside the script to record intermediate
state (resolved owner/repo, API call URL and response status,
comment-mapping decisions). A complete diagnostic story
requires the upstream package to emit structured logs of its
own.

## Goals

- **Observability without source access.** Operators must enable
  verbose diagnostics via configuration alone, never by editing
  the installed package.
- **Stable, parseable log format.** Log lines must remain
  greppable and tail-friendly by default, with optional
  structured (JSON) output for machine consumption.
- **Per-event correlation.** A single Gerrit event must produce
  a recognisable run of log lines that an operator can slice
  out of a shared log file.
- **No sensitive data leakage.** Tokens, full request bodies,
  and authorisation headers must never appear in logs even at
  the most verbose level.
- **Backwards-compatible.** Existing deployments at LF must see
  no behaviour change unless they explicitly opt in via the new
  configuration surface.
- **Standard library only.** Logging must rely on Python's
  `logging` module and the existing `gerrit_to_platform.ini`
  parser. No new runtime dependencies.

## Non-Goals

- Functional changes to dispatch logic, comment-mapping
  resolution, or platform detection.
- A web UI, dashboard, or metrics export. Logs remain the
  artefact; downstream consumers (Loki, Grafana, etc.) handle
  the rest.
- Replacing the `rich`-based traceback formatting on uncaught
  exceptions. That stays as a final-resort, human-readable
  signal.
- Adding distributed tracing (OpenTelemetry, Jaeger, etc.).
  Logs with correlation IDs cover the scale we operate at;
  tracing adds engineering cost without proportionate benefit.

## Current State Analysis

The implementer should produce this section after a structured
read of the upstream tree at
`gerrit.linuxfoundation.org/infra/admin/repos/releng/gerrit_to_platform`.
The audit covers:

- Every `print()`, `sys.stderr.write()`, and similar
  direct-output call site, with the event it reports and the
  level it morally maps to (DEBUG / INFO / WARNING / ERROR).
- Any pre-existing uses of the `logging` module (most likely
  none beyond the `rich.logging.RichHandler` traceback hook).
- Every external boundary: subprocess invocations, REST calls,
  file reads/writes. Each boundary becomes a candidate site for
  a structured log line.
- Configuration surfaces (env vars, INI keys, command-line
  flags) the package already honours.

The audit feeds the implementation plan below. Until that
audit lands, the call-site list in
[Section 8](#8-hook-lifecycle-instrumentation) and
[Section 9](#9-github-api-instrumentation) reads as
illustrative rather than exhaustive.

## Design Principles

1. **Single root logger** named `gerrit_to_platform` with all
   internal modules deriving via `logging.getLogger(__name__)`.
2. **No module-level configuration.** Logging is configured
   once, at the start of each hook entry-point, from the union
   of environment variables and INI settings.
3. **Behaviour parity at default level.** With no configuration
   changes the package's stdout/stderr footprint is byte-equal
   to today's output. Verbose logging is purely additive.
4. **Fail open.** If the log destination cannot be opened (file
   permission, missing directory, etc.) the package falls back
   to stderr and continues; it never aborts a hook because
   logging broke.
5. **Idempotent setup.** Hook entry-points may be invoked many
   times in a single process (in tests, or hypothetically in a
   long-running daemon) without duplicating handlers or
   re-installing the formatter.

## Proposed Design

### 1. Adopt the Standard `logging` Module

Introduce `gerrit_to_platform/_logging.py` exposing a single
`configure(...)` function called from each hook entry-point
(`patchset_created.py`, `comment_added.py`, `change_merged.py`)
before any other work. Every other module obtains its logger via
`logger = logging.getLogger(__name__)`; no module touches
handlers, formatters, or filters directly.

### 2. Per-Module Loggers

Logger names follow the package layout so operators can target
specific subsystems:

| Logger name                              | Responsibility                       |
| ---------------------------------------- | ------------------------------------ |
| `gerrit_to_platform`                     | Root; package-wide settings          |
| `gerrit_to_platform.patchset_created`    | Patchset-created hook entry-point    |
| `gerrit_to_platform.comment_added`       | Comment-added hook entry-point       |
| `gerrit_to_platform.change_merged`       | Change-merged hook entry-point       |
| `gerrit_to_platform.helpers`             | Argv parsing, change-id extraction   |
| `gerrit_to_platform.config`              | INI loading, validation              |
| `gerrit_to_platform.platform_detection`  | Replication-config remote scan       |
| `gerrit_to_platform.github`              | GitHub API client (workflow lookup)  |
| `gerrit_to_platform.github.dispatch`     | Workflow dispatch operations         |
| `gerrit_to_platform.gitlab`              | GitLab equivalent (when implemented) |

Operators raise just one subsystem when they need it:

```bash
G2P_LOG_LEVEL=INFO
G2P_LOG_LEVEL_OVERRIDES=gerrit_to_platform.github=DEBUG
```

### 3. Configuration Surface

Configuration is read in this precedence (highest first):

1. Environment variables (`G2P_LOG_*`).
2. `[logging]` section in `gerrit_to_platform.ini`.
3. Built-in defaults (WARNING to stderr only).

Recognised settings:

| Env var / INI key       | Type    | Default   | Description                                                                  |
| ----------------------- | ------- | --------- | ---------------------------------------------------------------------------- |
| `level`                 | string  | `WARNING` | Root level (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`)                     |
| `level_overrides`       | string  | empty     | `name=LEVEL,name=LEVEL,…` per-logger overrides                               |
| `format`                | string  | `text`    | `text` (human-readable) or `json` (one JSON object per line)                 |
| `file`                  | path    | empty     | Optional secondary file sink; primary remains stderr                         |
| `file_mode`             | string  | `append`  | `append` or `overwrite`                                                      |
| `redact_request_bodies` | bool    | `true`    | When `false`, request bodies are logged at DEBUG (still scrubbed of secrets) |
| `correlation_id_env`    | string  | empty     | If set, value is read from this env var as the parent correlation id         |

Environment variables use the prefix `G2P_LOG_` and an uppercase
suffix (e.g. `G2P_LOG_LEVEL`, `G2P_LOG_FORMAT`,
`G2P_LOG_FILE`). The INI section is `[logging]` with the
lower-case keys above.

### 4. Log Format and Structure

The default `text` format is:

```text
2026-05-12T14:30:00Z [INFO ] [cid=abc123] gerrit_to_platform.patchset_created: dispatching workflow gerrit-verify.yaml on modeseven-gerrit-onap/ccsdk-apps
```

Field layout: `{ts} [{level:<5}] [{cid=}] {logger}: {message}`.
The leading column widths are fixed so streams remain readable
in a terminal.

The `json` format produces one JSON object per line:

```json
{"ts":"2026-05-12T14:30:00Z","level":"INFO","cid":"abc123","logger":"gerrit_to_platform.patchset_created","message":"dispatching workflow gerrit-verify.yaml on modeseven-gerrit-onap/ccsdk-apps","extra":{"event":"patchset-created","change_number":"42"}}
```

`extra` carries any structured context attached via the standard
`logger.info("…", extra={…})` mechanism.

### 5. Correlation IDs

Each hook invocation generates a short correlation id (8-char
URL-safe base64 of the hook's start time + `os.urandom`). The id
is attached to a `logging.LoggerAdapter` and rendered as the
`cid=` field in the text format and the `cid` key in JSON.

If `correlation_id_env` names an environment variable that is
already set when the hook runs (e.g. by an enclosing wrapper
that already minted one), that value is used verbatim instead of
generating a new id. This lets the `gerrit-action` hook wrapper
adopt the same id so a single Gerrit event is greppable across
both the wrapper log and the gerrit_to_platform log.

### 6. Sinks

- **Primary**: `stderr`. Always present. Gerrit's `hooks` plugin
  captures it into `error_log`.
- **Secondary (optional)**: a file sink, opened in either
  `append` or `overwrite` mode at `configure()` time. Used for
  long-lived deployments where stderr is not retained.

A future enhancement could add a syslog handler. It is not in
scope for the initial work.

### 7. Sensitive Data Handling

A `RedactingFilter` is attached to every handler:

- Drops the `Authorization` header from any captured request.
- Replaces `token=` query-string values with `token=***`.
- Truncates request bodies to 1 KiB and replaces any string
  matching `gh[ps]_[A-Za-z0-9]{36,}` with `***`.
- The filter is applied **before** any handler sees the record,
  so a future syslog or remote handler cannot leak.

Tests assert that with `redact_request_bodies=false` and a
synthetic token in the body, no log line ever contains the
token.

### 8. Hook Lifecycle Instrumentation

Each hook entry-point emits, at INFO unless noted:

| Stage               | Level | Example                                                                              |
| ------------------- | ----- | ------------------------------------------------------------------------------------ |
| Entry               | INFO  | `hook=patchset-created argv=[…] cwd=/var/gerrit user=gerrit`                         |
| Config loaded       | INFO  | `config_path=/var/gerrit/.config/gerrit_to_platform/gerrit_to_platform.ini`          |
| Event parsed        | DEBUG | `change_number=42 change_id=I0a… project=ccsdk/apps branch=master patchset=1`        |
| Platform detected   | INFO  | `platform=github owner=modeseven-gerrit-onap repo=ccsdk-apps style=dash`             |
| Workflow lookup     | INFO  | `workflow_filter=verify candidates=[gerrit-verify.yaml] selected=gerrit-verify.yaml` |
| Dispatch attempt    | INFO  | `POST /repos/.../dispatches inputs={GERRIT_BRANCH:…} (12 keys)`                      |
| Dispatch success    | INFO  | `dispatch_status=204`                                                                |
| Dispatch failure    | ERROR | `dispatch_status=404 body=Workflow not found`                                        |
| Skipped (no match)  | INFO  | `no workflow matched filter=verify on owner/repo`                                    |
| Comment-mapping hit | INFO  | `comment_keyword=recheck mapped_filter=verify`                                       |
| Exit                | INFO  | `exit_code=0 elapsed_ms=842`                                                         |

A normal successful dispatch produces ~6 INFO lines; a
no-workflow-matched event produces ~5; a failure produces the
same plus an ERROR. DEBUG adds the parsed event payload and any
intermediate decision logic.

### 9. GitHub API Instrumentation

The HTTP client wrapper logs every outbound request at DEBUG
and every non-2xx response at WARNING:

| Field             | Notes                                                |
| ----------------- | ---------------------------------------------------- |
| `method`          | `GET` / `POST` / etc.                                |
| `url`             | Full URL with `Authorization` header redacted        |
| `status`          | HTTP status code                                     |
| `elapsed_ms`      | Wall-clock time                                      |
| `request_id`      | GitHub's `X-GitHub-Request-Id` response header       |
| `rate_limit_left` | `X-RateLimit-Remaining` (helps explain 403s)         |
| `body_summary`    | First 200 chars of response on non-2xx, redacted     |

The wrapper deliberately does **not** log full response bodies
on success — they can run to many KB and add no diagnostic
value once the status code is known.

### 10. Backwards Compatibility

- Default level is `WARNING`, so existing deployments that have
  not configured anything see only the same errors they see
  today (plus correlation ids on those error lines).
- All `print(...)` calls that are part of the user-facing
  contract (e.g. dispatch confirmation messages echoed to the
  Gerrit hook stdout) remain `print(...)` calls. They are
  parallel to, not replaced by, the new logging stream.
- The `rich`-formatted uncaught-exception traceback continues
  to fire on truly unexpected errors; the new logger emits an
  ERROR record with the same exception via
  `logger.exception(...)` immediately before re-raising.
  Operators choose which to consume.
- The new INI section is optional. Packages that have never
  written `[logging]` continue to work unchanged.

## Implementation Plan

### Phase 0: Audit

- Read the current `gerrit_to_platform` source tree end-to-end
  and produce the call-site map promised in
  [Current State Analysis](#current-state-analysis).
- Open a Gerrit change for the audit document so reviewers can
  agree on the call-site classification before code changes
  start.

### Phase 1: Foundation

- Add `gerrit_to_platform/_logging.py` with `configure()`,
  `RedactingFilter`, the text and JSON formatters, and the
  correlation-id `LoggerAdapter` factory.
- Add `gerrit_to_platform/_config_logging.py` to parse the new
  `[logging]` INI section and the `G2P_LOG_*` env vars.
- Wire `_logging.configure()` into each hook entry-point as the
  first non-trivial line.
- Tests cover precedence (env over INI over default), fallback
  on broken file sink, and idempotent `configure`.

### Phase 2: Hook Lifecycle Logs

- Replace existing `print(...)` calls in
  `patchset_created.py`, `comment_added.py`, and
  `change_merged.py` with the matching `logger.info(...)` calls
  per [Section 8](#8-hook-lifecycle-instrumentation).
- Keep any user-facing `print(...)` that is part of the
  dispatch confirmation contract.
- Tests use `caplog` to assert the expected line set per
  scenario (success, no-match, dispatch failure, malformed
  event).

### Phase 3: GitHub API Wrapper

- Wrap the existing HTTP entry-points (`fastcore.net.urlsave`,
  `urlread`, etc.) in a thin client class that emits the
  records described in
  [Section 9](#9-github-api-instrumentation).
- Add the `RedactingFilter` and assert via test that a
  synthetic token never reaches a handler.

### Phase 4: Documentation

- Update `gerrit_to_platform`'s `README.md` with the new
  configuration surface and a worked example.
- Add a `LOGGING.md` developer guide describing the logger
  hierarchy, correlation-id flow, and how to add new
  instrumentation in future patches.
- Update `gerrit-action/docs/G2P-CONFIGURATION.md` to point at
  the new INI section and to deprecate the wrapper-script-only
  guidance once the upstream package version that ships this
  work becomes the action's pinned default.

### Phase 5: gerrit-action Integration

- Generate a `[logging]` block in `gerrit_to_platform.ini` from
  the action's `g2p_*` inputs (new inputs: `g2p_log_level`,
  `g2p_log_format`).
- Have the action's hook wrapper export `G2P_CORRELATION_ID`
  per-invocation so the upstream picks up the same id, giving a
  single greppable thread per Gerrit event across both logs.
- Add a self-test check that
  `G2P_LOG_LEVEL=DEBUG ... --help` emits at least one DEBUG
  record (proves the log pipeline is wired).

### Phase 6: Release

- Tag a `gerrit_to_platform` release and publish to PyPI.
- Update `gerrit-action/docker/requirements.txt` to pin the new
  release.
- Update integration test workflows to assert log content.

## Testing Strategy

- **Unit**: `pytest` covers each helper in `_logging.py` and
  the precedence/redaction logic.
- **Integration**: A small fixture spins each hook entry-point
  in-process with a mocked GitHub client, captures the log
  stream, and asserts the expected line set per scenario.
- **End-to-end**: A new test workflow in `gerrit-action`'s
  `testing.yaml` deploys a Gerrit container, configures G2P
  with `G2P_LOG_LEVEL=DEBUG`, fires a synthetic hook, and
  asserts the log file contains the entry/dispatch/exit
  triplet.
- **Redaction**: A dedicated test asserts that with a known
  token set in the INI, no captured log line contains the token
  bytes at any level.

## Migration and Rollout

Because all behaviour is opt-in beyond the default WARNING
level, no migration is required. Existing deployments continue
to see the same output until they choose to enable the new
verbosity. The rollout sequence is:

1. Phases 0-3 land in `gerrit_to_platform`. New release is
   published, but `gerrit-action` does not bump its pin yet —
   default behaviour is unchanged.
2. Phase 4 documentation lands in both repos.
3. `gerrit-action` bumps the pin (Phase 5) and exposes the new
   inputs. Operators that want diagnostics opt in.
4. After one calendar quarter at the new minimum version, the
   wrapper script in `gerrit-action`
   (`/var/gerrit/hooks/<hook>` tee) can be retired in favour of
   the upstream's own log sink. The wrapper stays in the repo
   as a fallback for the "operator wants per-event argv capture
   even with logging disabled" case.

## Operational Playbook

After this work lands, the standard troubleshooting flow
becomes:

1. Reproduce the event (push a change, leave a comment, etc.).
2. `tail -F /var/gerrit/logs/gerrit_to_platform.log`.
3. Grep for the correlation id printed by the `gerrit-action`
   wrapper header.
4. Read the structured trace: entry → config → event parsed →
   platform detected → workflow lookup → dispatch attempt →
   dispatch result → exit.

If the trace is missing entirely, the hooks plugin did not
invoke the script (problem is in Gerrit or the wrapper). If the
trace ends before "platform detected", the issue is in
`replication.config` or the INI. If it ends at "workflow
lookup", the GitHub repo or workflow is misconfigured. If the
dispatch attempt logs a non-2xx status, the failure is on
GitHub's side and the response body summary points at the
cause.

## Open Questions

1. **Where does the upstream package live for review?** The
   upstream is hosted at
   `gerrit.linuxfoundation.org/infra/admin/repos/releng/gerrit_to_platform`.
   This work needs to land via Gerrit code review with the LF
   release engineering team. Is there an existing process or
   contact for non-trivial enhancements?
2. **Log file location default.** Should the file sink default
   to `/var/gerrit/logs/gerrit_to_platform.log`, or somewhere
   under the user's `~/.local/state/gerrit_to_platform/`?
   Container deployments prefer the former; bare-metal
   long-running production deployments may prefer the latter.
3. **JSON schema versioning.** If we commit to a JSON line
   format, do we add a `schema_version` field for downstream
   consumers? Likely yes, set to `"1"` initially.
4. **Per-hook `--debug` CLI flag.** Add a `--debug` flag to
   each hook entry-point that overrides level for one
   invocation? Probably yes; it costs nothing and helps
   operators reproduce a single hook manually.
5. **Rate-limit observation.** Add a separate logger that fires
   WARNING when `X-RateLimit-Remaining` drops below 100? Useful
   for fleet operations but tangential to per-event debugging.
   Defer to a follow-up.

## References

- `gerrit-action/docs/G2P-CONFIGURATION.md` — in-container
  configuration model.
- `gerrit-action/docs/GITHUB-ORG-VERIFY-CONFIG.md` — org-side
  audit and provisioning.
- `gerrit-action/scripts/lib/g2p_setup.py` — the function
  `_build_hook_wrapper` shows the correlation-id contract the
  wrapper expects to share with the upstream.
- Python `logging` HOWTO:
  <https://docs.python.org/3/howto/logging.html>
- Twelve-Factor logs: <https://12factor.net/logs>
