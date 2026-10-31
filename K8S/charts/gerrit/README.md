<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Gerrit Helm chart

Runs one Gerrit primary per release as a single-replica `StatefulSet`
with its site on a persistent volume. Gerrit's configuration comes
from chart values: Helm renders `etc/gerrit.config`, and
`etc/secure.config` is written at pod start from Secrets, so the site
volume holds state only.

## What it deploys

| Resource                          | Purpose                                                        |
| --------------------------------- | -------------------------------------------------------------- |
| `StatefulSet` `<fullname>`        | Gerrit; site PVC `site` mounted at `/var/gerrit`               |
| `Service` `<fullname>-http`       | ClusterIP, port 8080                                           |
| `Service` `<fullname>-ssh`        | Port 29418, type `LoadBalancer`; only with `ssh.service.enabled` |
| `Service` `<fullname>-headless`   | Governing service of the `StatefulSet`                         |
| `ConfigMap` `<fullname>-scripts`  | `files/scripts/*.sh`                                           |
| `ConfigMap` `<fullname>-config`   | Rendered `gerrit.config`, `jgit.config` and `notedb.config`    |
| `Secret` `<fullname>-admin`       | Admin credential placeholder; not consumed yet                 |
| `Secret` `<fullname>-secure-config` | Values `render-config.sh` writes to `etc/secure.config`; see the Secret contract |
| `NetworkPolicy` (two)             | Default deny; allow ingress to 8080 and 29418, egress to DNS, HTTPS and, with mail configured, the SMTP relay's port |

The Secrets carry `helm.sh/resource-policy: keep` and
`argocd.argoproj.io/sync-options: Prune=false,Delete=false`, so they
survive `helm uninstall` and Argo CD pruning. A Helm upgrade reuses
the values already in the cluster rather than generating new ones.

That reuse depends on Helm's `lookup`, which returns nothing when a
chart renders offline. Argo CD renders that way, so each sync would
generate and apply new values, and a new `registerEmailPrivateKey`
invalidates outstanding email verification links. Under GitOps, create
the Secrets outside the chart, for example with an external secrets
operator or a sealed Secret, holding the keys the table below names,
and set `secrets.admin.existingSecret` and
`secrets.secureConfig.existingSecret` to their names. The chart then
renders neither Secret, and `render-config` reads the named
secure-config Secret.

### Secret contract

| Value                                 | Key                       | Required | Written to                                    |
| ------------------------------------- | ------------------------- | -------- | --------------------------------------------- |
| `secrets.admin.existingSecret`        | `username`, `password`    | Yes      | Not consumed yet                              |
| `secrets.secureConfig.existingSecret` | `registerEmailPrivateKey` | Yes      | `auth.registerEmailPrivateKey` in `secure.config` |
| `secrets.secureConfig.existingSecret` | `smtpPass`                | No       | `sendemail.smtpPass` in `secure.config`       |

The generated `<fullname>-secure-config` Secret carries a random
`registerEmailPrivateKey` and an empty `smtpPass`. Fill `smtpPass` in
the cluster when the mail relay needs a password; upgrades keep it. An
empty or absent optional key leaves its entry out of `secure.config`.

## Start-up sequence

1. `volume-permissions` (only with
   `persistence.volumePermissions.enabled`) chowns anything on the site
   volume the `gerrit` user (UID 1001) does not own.
2. `site-init` copies `gerrit.war` and `gerrit.sh` from the image,
   replaces the site's plugin and library jars with those named in
   `pluginCatalogue`, and copies the rendered `gerrit.config`,
   `jgit.config` and `notedb.config` over the site's own. On a site
   without the repository `gerrit.allProjects` names (`All-Projects`
   unless `gerrit.extraConfig` sets it) it then runs
   `gerrit init --batch --no-auto-start` on that configuration and
   installs the rendered files again over whatever init added.
3. `render-config` rewrites `etc/secure.config` from the Secrets,
   creating it with mode `0600` before writing any value.
4. `gerrit` runs `bin/gerrit.sh run`. A startup probe allows ten
   minutes; readiness and liveness probes call
   `<urlPrefix>/config/server/version`.

The official Gerrit image declares `cache`, `db`, `etc`, `git` and
`index` under `/var/gerrit` as image volumes. The chart mounts each one
from the site PVC by `subPath`; without that, the container runtime
mounts empty anonymous volumes over them and the site disappears.

## Values

| Value                                    | Default                          | Notes                                                      |
| ---------------------------------------- | -------------------------------- | ---------------------------------------------------------- |
| `image.repository`                       | `ghcr.io/lfreleng-actions/gerrit` | Placeholder until the published image exists              |
| `image.tag`                              | `""`                             | Defaults to the chart `appVersion`                         |
| `image.digest`                           | `""`                             | Takes precedence over the tag when set                     |
| `gerrit.host`                            | `gerrit.example.org`             | Host part of `gerrit.canonicalWebUrl`                      |
| `gerrit.urlPrefix`                       | `/r`                             | Served path prefix; `""` serves from the root              |
| `gerrit.instanceId`                      | `""`                             | `gerrit.instanceId`; left out when empty                   |
| `gerrit.serverId`                        | `""`                             | See [Server ID](#server-id)                                |
| `gerrit.extraConfig`                     | `""`                             | Appended verbatim to `gerrit.config`                       |
| `gerrit.webScheme`                       | `https`                          | Scheme of `gerrit.canonicalWebUrl`                         |
| `gerrit.listenScheme`                    | `proxy-https`                    | `httpd.listenUrl` scheme; `http` for direct access         |
| `pluginCatalogue.path`                   | `/var/gerrit-catalogue`          | Image directory holding `plugins/` and `lib/`              |
| `pluginCatalogue.plugins` / `.libs`      | `[]`                             | Jar names without `.jar`; a missing one fails `site-init`  |
| `persistence.storageClassName`           | `""`                             | Cluster default when empty                                 |
| `persistence.size`                       | `20Gi`                           |                                                            |
| `persistence.volumePermissions.enabled`  | `false`                          | See below                                                  |
| `resources`                              | 500m / 2Gi, 4Gi limit            |                                                            |
| `secrets.admin.existingSecret`           | `""`                             | Pre-created Secret with `username` and `password`          |
| `secrets.secureConfig.existingSecret`    | `""`                             | Pre-created Secret with `registerEmailPrivateKey`          |
| `ssh.advertisedAddress`                  | `""`                             | Defaults to `gerrit.host`, without its port, on port 29418 |
| `ssh.service.enabled`                    | `false`                          | Adds the SSH `Service`                                     |
| `networkPolicy.enabled`                  | `true`                           |                                                            |

### gerrit.config

Helm renders the whole of `etc/gerrit.config` from
`files/conf/gerrit.config.tpl`, so `helm template` shows the
configuration Gerrit runs with, and `site-init` replaces the site's
file with it at every start. Edits made on the volume do not survive a
restart.

The chart derives these keys, and they win over the same key in
values:

| Key                         | Value                                                         |
| --------------------------- | ------------------------------------------------------------- |
| `gerrit.basePath`           | `git`                                                         |
| `gerrit.canonicalWebUrl`    | `<gerrit.webScheme>://<gerrit.host><gerrit.urlPrefix>/`       |
| `httpd.listenUrl`           | `<gerrit.listenScheme>://*:8080<gerrit.urlPrefix>/`           |
| `sshd.advertisedAddress`    | `ssh.advertisedAddress`, else `gerrit.host` on port 29418     |
| `cache.directory`           | `cache`                                                       |
| `container.javaOptions`     | The options the image needs, then `-XX:MaxRAMPercentage=70.0` unless `container.heapLimit` is set or there is no memory limit, then `container.javaOptions` from values |

Each of these top-level values renders the `gerrit.config` section of
the same name, key for key, with the defaults `values.yaml` lists:
`httpd`, `sshd`, `auth`, `sendemail`, `container`, `cache`, `change`,
`receive`, `download`, `index`, `gc`, `plugins`, `user`, `trackingid`,
`accounts` and `suggest`. A key set to `null` or `""` is left out, so
Gerrit's own default applies; a list repeats the key; a map renders a
subsection, as in `cache.diff_summary.diskLimit` becoming
`[cache "diff_summary"]`. Notable defaults:

| Value                       | Default                          | Notes                                                    |
| --------------------------- | -------------------------------- | -------------------------------------------------------- |
| `auth.type`                 | `DEVELOPMENT_BECOME_ANY_ACCOUNT` | Smoke default only; never use it in production           |
| `sendemail.enable`          | `false`                          | Gerrit's default relays through `localhost`, which the pod lacks |
| `sshd.listenAddress`        | `*:29418`                        |                                                          |
| `container.user`            | `gerrit`                         |                                                          |
| `cache.*.diskLimit`         | `1g`                             | For `diff_summary`, `gerrit_file_diff`, `git_file_diff` and `modified_files` |
| `plugins.allowRemoteAdmin`  | `false`                          | Plugins come from `pluginCatalogue`, not remote installs |

`jgit` and `notedb` render `etc/jgit.config` and `etc/notedb.config`
the same way, each top-level key a section. Secrets never go in
values: `auth.registerEmailPrivateKey` and `sendemail.smtpPass` come
from the [Secret contract](#secret-contract).

`gerrit.extraConfig` is appended verbatim, for settings with no typed
value. A key there that a typed value also sets adds a second entry
rather than replacing the first, so set such keys through the typed
value.

### Server ID

`gerrit.serverId` is site identity: Gerrit stamps it on every change
and refuses to load changes stamped with an ID it does not know. With
the value empty, Gerrit generates an ID on a new site and `site-init`
carries the site's ID into every later render. `site-init` stops
rather than read an unreadable or malformed `etc/gerrit.config` as
holding no ID. With the value set, `site-init` fails before Gerrit
starts when the site already holds a different ID, and on a new site
`gerrit init` takes it as the site's ID. When adopting an existing
site, copy the ID from its `etc/gerrit.config`.

### Volume permissions

The pod runs as UID and GID 1001 with `fsGroup` and
`fsGroupChangePolicy: OnRootMismatch`, a read-only root filesystem,
every capability dropped and no service account token, so it meets the
`restricted` Pod Security level. Some provisioners, kind's
`local-path` among them, ignore `fsGroup`. For those,
`persistence.volumePermissions.enabled` adds an init container that
runs as root with `CHOWN` and `DAC_OVERRIDE`. That container breaks
the `restricted` level but stays within `baseline`, so enable it only
where the namespace allows `baseline` or less, as in local smoke tests.

## Quick validation

Build the image from this repository's `Dockerfile` until a published
image exists, then:

```sh
docker build -t gerrit-local:dev .
helm lint K8S/charts/gerrit
helm template gerrit K8S/charts/gerrit --namespace gerrit-smoke \
  -f K8S/charts/gerrit/values-local-smoke.yaml
kind create cluster
kind load docker-image gerrit-local:dev
helm install gerrit K8S/charts/gerrit -n gerrit-smoke --create-namespace \
  -f K8S/charts/gerrit/values-local-smoke.yaml --wait --timeout 10m
kubectl -n gerrit-smoke port-forward svc/gerrit-http 8080 &
curl -s http://localhost:8080/r/config/server/version   # prints the release
git clone http://localhost:8080/r/All-Projects          # an empty checkout
kubectl -n gerrit-smoke delete pod gerrit-0
kubectl -n gerrit-smoke rollout status sts/gerrit       # site survives
```

`All-Projects` holds only `refs/meta/config`, so the clone succeeds
with an "empty repository" warning.

`files/scripts/verify-site.py` runs the last four checks itself, port
forwarding the Service and proving persistence with a marker project
it creates before deleting the pod. It also checks that
`<urlPrefix>/config/server/info` reports the authentication type
given with `--auth-type`, and each download scheme given with
`--download-scheme`, by the name server info reports (`ssh`, `http`,
`anonymous http`). Download schemes come from the `download-commands`
plugin, so name them only when `pluginCatalogue` installs it. The
script needs only python3, `kubectl` and `git`, and stays out of the
scripts `ConfigMap`, which takes `*.sh` alone:

```sh
python3 K8S/charts/gerrit/files/scripts/verify-site.py \
  --namespace gerrit-smoke --fullname gerrit --url-prefix /r \
  --auth-type DEVELOPMENT_BECOME_ANY_ACCOUNT
```

The `chart-smoke` job in `.github/workflows/testing.yaml` runs this
sequence on every pull request. It also lints and renders the chart
with each values file under `examples/`. With `image.digest` set, it
parses each render with `scripts/lib/chart_digests.py` and checks
that every container, init container and ephemeral container image
ends in that digest.

`tests/chart/` holds golden tests: each values file under `examples/`
must render the `gerrit.config`, `jgit.config` and `notedb.config` in
`tests/chart/golden/<example>/`, and `render-config.sh` must write the
`secure.config` there with mode `0600`. After an intended change to
the rendered output, regenerate the goldens and review the diff:

```sh
UPDATE_GOLDEN=1 uv run --extra test pytest tests/chart
```
