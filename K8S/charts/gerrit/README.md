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
| `Service` `<fullname>-ssh`        | Port 29418, type `LoadBalancer`, `externalTrafficPolicy: Local`; with `ssh.service.enabled` (the default) |
| `Service` `<fullname>-headless`   | Governing service of the `StatefulSet`                         |
| `Ingress` `<fullname>`            | With `ingress.enabled`; see [Exposing Gerrit](#exposing-gerrit) |
| `IngressRoute` `<fullname>`, `Middleware`s, `Certificate` `<fullname>-tls` | Instead of the `Ingress`, with `ingress.traefik.enabled` |
| `ConfigMap` `<fullname>-scripts`  | `files/scripts/*.sh`                                           |
| `ConfigMap` `<fullname>-config`   | Rendered `gerrit.config`, `jgit.config` and `notedb.config`    |
| `Secret` `<fullname>-admin`       | Admin credential placeholder; not consumed yet                 |
| `Secret` `<fullname>-secure-config` | Values `render-config.sh` writes to `etc/secure.config`; see the Secret contract |
| `NetworkPolicy` (two)             | Default deny; allow ingress to 8080 (from `networkPolicy.ingressNamespaceSelector` when set) and 29418, egress to DNS, HTTPS and, with mail configured, the SMTP relay's port |

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
| `gerrit.webScheme`                       | `https`                          | Scheme of `gerrit.canonicalWebUrl`; ignored with `ingress.enabled` |
| `gerrit.listenScheme`                    | `proxy-https`                    | `httpd.listenUrl` scheme; `http` for direct access; ignored with `ingress.enabled` |
| `ingress.enabled`                        | `false`                          | Renders the `Ingress`, or the Traefik objects              |
| `ingress.className`                      | `""`                             | `ingressClassName`; `nginx` adds the ingress-nginx annotations |
| `ingress.annotations`                    | `{}`                             | Added to the chart's own; a key here wins                  |
| `ingress.tls.enabled`                    | `true`                           | TLS ends at the controller                                 |
| `ingress.tls.secretName`                 | `""`                             | Defaults to `<fullname>-tls`                               |
| `ingress.tls.issuer`                     | `letsencrypt-prod`               | cert-manager `ClusterIssuer`; `""` to create the Secret yourself |
| `ingress.redirectRootToPrefix`           | `true`                           | Redirects `/` to `<gerrit.urlPrefix>/`                     |
| `ingress.securityHeaders.enabled`        | `true`                           | Traefik only; see below                                    |
| `ingress.securityHeaders.contentSecurityPolicy` | `""`                      | `""` for the chart's default policy                        |
| `ingress.traefik.enabled`                | `false`                          | `IngressRoute` and `Middleware`s instead of an `Ingress`   |
| `ingress.traefik.entryPoints`            | `[]`                             | Traefik's default entry points when empty                  |
| `pluginCatalogue.path`                   | `/var/gerrit-catalogue`          | Image directory holding `plugins/` and `lib/`              |
| `pluginCatalogue.plugins` / `.libs`      | `[]`                             | Jar names without `.jar`; a missing one fails `site-init`  |
| `persistence.storageClassName`           | `""`                             | Cluster default when empty                                 |
| `persistence.size`                       | `20Gi`                           |                                                            |
| `persistence.volumePermissions.enabled`  | `false`                          | See below                                                  |
| `resources`                              | 500m / 2Gi, 4Gi limit            |                                                            |
| `secrets.admin.existingSecret`           | `""`                             | Pre-created Secret with `username` and `password`          |
| `secrets.secureConfig.existingSecret`    | `""`                             | Pre-created Secret with `registerEmailPrivateKey`          |
| `ssh.host`                               | `""`                             | SSH host clients use; defaults to `gerrit.host` without its port |
| `ssh.port`                               | `null`                           | SSH port clients use; see [SSH](#ssh)                      |
| `ssh.service.enabled`                    | `true`                           | Adds the SSH `Service`                                     |
| `ssh.service.type`                       | `LoadBalancer`                   |                                                            |
| `ssh.service.nodePort`                   | `null`                           | Fixed node port with type `NodePort`                       |
| `ssh.service.annotations`                | `{}`                             | For example the AWS NLB annotations below                  |
| `ssh.service.externalTrafficPolicy`      | `Local`                          | Set on `LoadBalancer` and `NodePort` Services only         |
| `ssh.service.externalDNS`                | `false`                          | Adds `external-dns.alpha.kubernetes.io/hostname: <ssh.host>` |
| `networkPolicy.enabled`                  | `true`                           |                                                            |
| `networkPolicy.ingressNamespaceSelector` | `{}`                             | Label selector for the namespaces that may reach 8080; `{}` admits any source |

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
| `gerrit.canonicalWebUrl`    | `<scheme>://<gerrit.host><gerrit.urlPrefix>/`; the scheme is `https` behind the chart's Ingress with TLS, `http` without, and `gerrit.webScheme` with no Ingress |
| `httpd.listenUrl`           | `<scheme>://*:8080<gerrit.urlPrefix>/`; the scheme is `proxy-https` behind the chart's Ingress with TLS, `proxy-http` without, and `gerrit.listenScheme` with no Ingress |
| `sshd.advertisedAddress`    | `ssh.host`, else `gerrit.host`, without any port, then `:` and the SSH port: `ssh.port`, else `ssh.service.nodePort` on a `NodePort` Service, else `29418` |
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
| `cache.limits`              | See [Cache limits](#cache-limits) | `diskLimit` of each persistent cache                    |
| `gc.startTime`, `gc.interval` | `Sun 03:00`, `1 week`          | See [Garbage collection](#garbage-collection)            |
| `plugins.allowRemoteAdmin`  | `false`                          | Plugins come from `pluginCatalogue`, not remote installs |

`jgit` and `notedb` render `etc/jgit.config` and `etc/notedb.config`
the same way, each top-level key a section. `jgit` defaults to
`receive.autogc: false`, which `gerrit init` would otherwise write and
the render would then replace. Secrets never go in
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

## Exposing Gerrit

HTTP goes through the cluster's Ingress controller to the
`<fullname>-http` Service; SSH goes through its own `<fullname>-ssh`
Service, because an HTTP Ingress cannot route Gerrit's SSH protocol.
The chart derives `gerrit.canonicalWebUrl`, `httpd.listenUrl` and
`sshd.advertisedAddress` from `gerrit.host`, `gerrit.urlPrefix`,
`ssh.host` and `ingress.tls.enabled`, so the links Gerrit builds match
what the Ingress and the SSH Service serve.

### Ingress

With `ingress.enabled`, the chart renders a `networking.k8s.io/v1`
`Ingress` for the host in `gerrit.host`, without its port, and the path
`gerrit.urlPrefix` (`/` for the root) with `pathType: Prefix`. It does
not rewrite the path: Gerrit expects the prefix in the request. With
`ingress.tls.enabled`, TLS ends at the controller with the certificate
in `ingress.tls.secretName`, and with `ingress.tls.issuer` set
cert-manager issues that Secret through the
`cert-manager.io/cluster-issuer` annotation.

Only ingress-nginx has annotations for the root redirect and for large
`git push` requests, so with `ingress.className: nginx` the chart adds
`app-root` for the redirect, with an `Exact` path for `/` so that the
Ingress receives the request it redirects, `proxy-body-size: "0"` and
one-hour read and send timeouts. For another controller, set its
equivalents in `ingress.annotations`, or use the Traefik objects
below. The security headers are Traefik-only for the same reason.

### Traefik

With `ingress.traefik.enabled`, the chart renders a Traefik
`IngressRoute` and `Middleware`s instead of the `Ingress`:

- a route for `Host(<host>)` and the prefix, matched by path segment;
- with `ingress.redirectRootToPrefix` and a prefix, a route for `/`
  through a `redirectRegex` Middleware to `<prefix>/`;
- with `ingress.securityHeaders.enabled`, a `headers` Middleware on
  both routes, ahead of the redirect so that the redirect carries the
  headers too, setting `Content-Security-Policy`,
  `Strict-Transport-Security` (with TLS), `Referrer-Policy`,
  `Permissions-Policy` and `X-Content-Type-Options: nosniff`;
- with TLS and an issuer, a cert-manager `Certificate`, since
  cert-manager does not read `IngressRoute`s.

The default `Content-Security-Policy` allows same-origin resources,
the inline scripts and styles the web UI and SAML's auto-post page
use, web workers from `blob:` URLs, which the web UI's syntax
highlighting creates, and avatar images from any HTTPS origin. Set
`ingress.securityHeaders.contentSecurityPolicy` to replace it.

The chart adds no buffering Middleware, so Traefik puts no limit on
the request body and streams a `git push` through. The read timeout
is the entry point's `respondingTimeouts.readTimeout`, which Traefik
v3 sets to 60 seconds by default. It belongs to Traefik's own static
configuration, not to the chart, so raise it on the entry point Gerrit
uses, for example to `0` or several hours, before pushing large
repositories over HTTP.

`ingress.className` becomes the `kubernetes.io/ingress.class`
annotation, which Traefik's `kubernetesCRD` provider filters on when it
has an `ingressClass` set; that key in `ingress.annotations` wins.

### SSH

`ssh.service.enabled` (the default) renders `<fullname>-ssh` on port
29418 as `ssh.service.type`, `LoadBalancer` by default, with
`externalTrafficPolicy: Local` so that `sshd_log` records each
client's address rather than a node's. With `Local`, only nodes running
the Gerrit pod pass the load balancer's health check, which is what a
single-replica StatefulSet needs. `sshd.advertisedAddress` is
`<ssh.host>:<port>`, with `ssh.host` defaulting to `gerrit.host`.

The port is `ssh.port` when set, for a proxy or load balancer that
publishes 29418 on another port. Otherwise a `NodePort` Service
advertises `ssh.service.nodePort`, which the chart also sets as the
Service's node port, and any other type advertises 29418. Kubernetes
picks an unset node port at random, so a `NodePort` Service with
neither value fails to render rather than advertise a port that does
not answer.

With `ssh.service.externalDNS`, the Service carries
`external-dns.alpha.kubernetes.io/hostname: <ssh.host>`. The Ingress
publishes `gerrit.host` through a different load balancer, so with both
enabled `ssh.host` has to name another host, such as
`ssh.gerrit.example.org`; the chart fails to render otherwise.

On AWS, an NLB through the AWS Load Balancer Controller needs:

```yaml
ssh:
  host: ssh.gerrit.example.org
  service:
    annotations:
      service.beta.kubernetes.io/aws-load-balancer-type: external
      service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
      service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing
      service.beta.kubernetes.io/aws-load-balancer-target-group-attributes: preserve_client_ip.enabled=true
    externalDNS: true
```

IP targets send traffic straight to the pod, bypassing
`externalTrafficPolicy`. The NLB then keeps the client address only
with the target group attribute `preserve_client_ip.enabled=true`,
which the last annotation sets; without it `sshd_log` records the
NLB's address.

### CDN or TCP proxy in front

A CDN or HTTP proxy in front of the Ingress works for HTTP when it
forwards the `Host` header and the path unchanged and sets
`X-Forwarded-Proto: https`; leave `gerrit.host` as the public name.
Raise its body size limit and read timeout as for the Ingress, or
large pushes over HTTP fail at the CDN.

An HTTP CDN cannot carry SSH. Point `ssh.host` at a name that resolves
to the SSH load balancer, or to a TCP (layer 4) proxy that forwards
port 29418 unchanged, not at the CDN's name. Leave the PROXY protocol
off on such a proxy unless the Gerrit release in use accepts it on its
SSH port: a PROXY header sent to a daemon that does not expect one
breaks every connection. Without it, `sshd_log` records the proxy's
address.

### Why TLS cannot end in front of SSH

Gerrit's SSH port speaks the SSH protocol, which runs its own key
exchange and encryption directly over TCP. A TLS terminator in front
of 29418 would expect a TLS handshake that SSH clients never send, and
would send TLS records that Gerrit's SSH daemon cannot read, so the
connection fails at once. A TLS-terminating HTTP load balancer or CDN
also routes by HTTP request, which SSH does not make. SSH therefore
needs a plain TCP path end to end, and its host key, not a TLS
certificate, proves the server's identity.

### Network policy

`networkPolicy.ingressNamespaceSelector` limits port 8080 to pods in
the namespaces it selects, for example the Ingress controller's:

```yaml
networkPolicy:
  ingressNamespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: traefik
```

Left empty, 8080 admits any source, as port 29418 always does: SSH
clients arrive from outside the cluster through the load balancer.

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

## Maintenance

A long-running site on a fixed-size volume needs its repositories
packed and its persistent caches bounded.

### Garbage collection

Gerrit's built-in scheduler runs `git gc` over every repository at
`gc.startTime`, then every `gc.interval`: weekly at 03:00 on Sunday,
server time, by default. `gc.aggressive` turns on aggressive mode.
`receive.autogc` stays `false` in `jgit.config`, so a push never
starts a gc of its own. Set `gc.startTime` and `gc.interval` to `null`
to turn the scheduler off.

### Cache limits

Gerrit keeps its persistent caches as H2 files under `cache/`. Once a
day, at 1 AM server time, it trims each cache over its `diskLimit`,
and the limit is 128 MiB for a cache that sets none. On a busy site
the diff caches can outgrow the git data, so the chart sets a limit
for each of these caches in `cache.limits`:

| Cache                | `diskLimit` |
| -------------------- | ----------- |
| `comment_context`    | `128m`      |
| `conflicts`          | `128m`      |
| `diff_intraline`     | `512m`      |
| `diff_summary`       | `1g`        |
| `gerrit_file_diff`   | `1g`        |
| `git_file_diff`      | `1g`        |
| `git_modified_files` | `512m`      |
| `modified_files`     | `1g`        |

Each entry renders `diskLimit` into the `[cache "<name>"]`
subsection. Override one in `cache.limits`, or drop it with `null` or
`""`; a `diskLimit` set in the subsection itself, as in
`cache.git_file_diff.diskLimit`, wins over `cache.limits`. A limit of
`0` turns disk storage off for that cache.

### Purging caches and stale indexes

Trimming keeps a cache within its limit, but an H2 file never shrinks
on disk, and every index version an upgrade leaves behind stays in
`index/` as `<name>_NNNN`. To reclaim the space, stop Gerrit, delete
the files from a pod that mounts the site volume, and start Gerrit
again. Gerrit rebuilds the caches as it needs them. The purge pod
below meets the `restricted` Pod Security Standard the chart's
namespace enforces:

```sh
ns=gerrit
kubectl -n "$ns" scale sts/gerrit --replicas=0
kubectl -n "$ns" wait --for=delete pod/gerrit-0 --timeout=5m
kubectl -n "$ns" run gerrit-purge --rm -i --restart=Never \
  --image=busybox:1.37 --overrides='{"spec": {
    "securityContext": {"runAsUser": 1001, "runAsGroup": 1001,
      "runAsNonRoot": true, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": "gerrit-purge", "image": "busybox:1.37",
      "command": ["sh", "-c", "rm -fv /site/cache/*.h2.db; ls /site/index"],
      "securityContext": {"allowPrivilegeEscalation": false,
        "capabilities": {"drop": ["ALL"]}},
      "volumeMounts": [{"name": "site", "mountPath": "/site"}]}],
    "volumes": [{"name": "site",
      "persistentVolumeClaim": {"claimName": "site-gerrit-0"}}]}}'
kubectl -n "$ns" scale sts/gerrit --replicas=1
```

`index/gerrit_index.config` names the version of each index Gerrit
uses, as `[index "changes_0084"]` with `ready = true`. Every other
`index/<name>_NNNN` directory is left over from an earlier release and
can go in the same step, for example by adding
`rm -rf /site/index/changes_0083` to the command above. Never delete
the versions `gerrit_index.config` marks ready: Gerrit would need an
offline reindex to rebuild them.

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
`tests/chart/golden/<example>/`, and `templates/ingress.yaml` as a
plain Ingress and as Traefik objects in `ingress-plain.golden` and
`ingress-traefik.golden` there, and `render-config.sh` must write the
`secure.config` there with mode `0600`. After an intended change to
the rendered output, regenerate the goldens and review the diff:

```sh
UPDATE_GOLDEN=1 uv run --extra test pytest tests/chart
```
