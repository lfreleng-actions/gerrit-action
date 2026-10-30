<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Gerrit Helm chart

Runs one Gerrit primary per release as a single-replica `StatefulSet`
with its site on a persistent volume. This is the chart skeleton: it
installs, becomes Ready and serves Git over HTTP. Configuration breadth
arrives in later changes.

## What it deploys

| Resource                          | Purpose                                                        |
| --------------------------------- | -------------------------------------------------------------- |
| `StatefulSet` `<fullname>`        | Gerrit; site PVC `site` mounted at `/var/gerrit`               |
| `Service` `<fullname>-http`       | ClusterIP, port 8080                                           |
| `Service` `<fullname>-ssh`        | Port 29418, type `LoadBalancer`; only with `ssh.service.enabled` |
| `Service` `<fullname>-headless`   | Governing service of the `StatefulSet`                         |
| `ConfigMap` `<fullname>-scripts`  | `files/scripts/*.sh`                                           |
| `ConfigMap` `<fullname>-config`   | `gerrit.config` values, passed to `site-init.sh` as env        |
| `Secret` `<fullname>-admin`       | Admin credential placeholder; not consumed yet                 |
| `Secret` `<fullname>-secure-config` | Values `render-config.sh` writes to `etc/secure.config`      |
| `NetworkPolicy` (two)             | Default deny; allow ingress to 8080 and 29418, egress to DNS and HTTPS |

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

## Start-up sequence

1. `volume-permissions` (only with
   `persistence.volumePermissions.enabled`) chowns anything on the site
   volume the `gerrit` user (UID 1001) does not own.
2. `site-init` runs `gerrit init --batch --no-auto-start` when
   `etc/gerrit.config` is absent, so a restart never re-initialises a
   site. At every start it copies `gerrit.war` and `gerrit.sh` from the
   image, replaces the site's plugin and library jars with those named
   in `pluginCatalogue`, and writes `gerrit.config` with `git config`.
3. `render-config` rewrites `etc/secure.config` from the Secrets.
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
| `gerrit.auth`                            | `DEVELOPMENT_BECOME_ANY_ACCOUNT` | Smoke default only; never use it in production             |
| `gerrit.webScheme`                       | `https`                          | Scheme of `gerrit.canonicalWebUrl`                         |
| `gerrit.listenScheme`                    | `proxy-https`                    | `httpd.listenUrl` scheme; `http` for direct access         |
| `pluginCatalogue.path`                   | `/var/gerrit-catalogue`          | Image directory holding `plugins/` and `lib/`              |
| `pluginCatalogue.plugins` / `.libs`      | `[]`                             | Jar names without `.jar`; a missing one fails `site-init`  |
| `persistence.storageClassName`           | `""`                             | Cluster default when empty                                 |
| `persistence.size`                       | `20Gi`                           |                                                            |
| `persistence.volumePermissions.enabled`  | `false`                          | See below                                                  |
| `resources`                              | 500m / 2Gi, 4Gi limit            |                                                            |
| `javaOptions`                            | `""`                             | When empty, the heap is 70% of `resources.limits.memory`   |
| `secrets.admin.existingSecret`           | `""`                             | Pre-created Secret with `username` and `password`          |
| `secrets.secureConfig.existingSecret`    | `""`                             | Pre-created Secret with `registerEmailPrivateKey`          |
| `ssh.advertisedAddress`                  | `""`                             | Defaults to `gerrit.host`, without its port, on port 29418 |
| `ssh.service.enabled`                    | `false`                          | Adds the SSH `Service`                                     |
| `networkPolicy.enabled`                  | `true`                           |                                                            |

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
it creates before deleting the pod. It needs only python3, `kubectl`
and `git`, and stays out of the scripts `ConfigMap`, which takes
`*.sh` alone:

```sh
python3 K8S/charts/gerrit/files/scripts/verify-site.py \
  --namespace gerrit-smoke --fullname gerrit --url-prefix /r
```

The `chart-smoke` job in `.github/workflows/testing.yaml` runs this
sequence on every pull request. It also lints and renders the chart
with each values file under `examples/`. With `image.digest` set, it
parses each render with `scripts/lib/chart_digests.py` and checks
that every container, init container and ephemeral container image
ends in that digest.
