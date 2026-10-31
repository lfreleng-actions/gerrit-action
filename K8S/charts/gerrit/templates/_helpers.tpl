{{/*
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
*/}}

{{/* Chart name, truncated to the 63-character DNS label limit */}}
{{- define "gerrit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release-qualified name; the release name alone when it already
holds the chart name. Capped at 52 characters: the StatefulSet
controller labels each revision "<name>-<hash>" within the 63-character
label limit, and that cap also leaves room for "-headless" on the
longest Service name. */}}
{{- define "gerrit.fullname" -}}
{{- $full := "" -}}
{{- if .Values.fullnameOverride -}}
{{- $full = .Values.fullnameOverride -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- $full = .Release.Name -}}
{{- else -}}
{{- $full = printf "%s-%s" .Release.Name $name -}}
{{- end -}}
{{- end -}}
{{- $full | trunc 52 | trimSuffix "-" -}}
{{- end -}}

{{- define "gerrit.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "gerrit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gerrit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "gerrit.labels" -}}
helm.sh/chart: {{ include "gerrit.chart" . }}
{{ include "gerrit.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gerrit
{{- end -}}

{{/* Image reference; a digest takes precedence over the tag */}}
{{- define "gerrit.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "gerrit.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ if kindIs "map" . }}{{ .name }}{{ else }}{{ . }}{{ end }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The gerrit user and group of the official Gerrit image */}}
{{- define "gerrit.uid" -}}1001{{- end -}}

{{- define "gerrit.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ include "gerrit.uid" . }}
runAsGroup: {{ include "gerrit.uid" . }}
fsGroup: {{ include "gerrit.uid" . }}
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "gerrit.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop:
    - ALL
{{- end -}}

{{/* Keep the OpenTelemetry operator from injecting agents */}}
{{- define "gerrit.otelOptOutAnnotations" -}}
instrumentation.opentelemetry.io/inject-java: "false"
instrumentation.opentelemetry.io/inject-nodejs: "false"
instrumentation.opentelemetry.io/inject-python: "false"
instrumentation.opentelemetry.io/inject-dotnet: "false"
instrumentation.opentelemetry.io/inject-go: "false"
instrumentation.opentelemetry.io/inject-sdk: "false"
{{- end -}}

{{/* Pre-created Secrets survive uninstall and Argo CD pruning */}}
{{- define "gerrit.keepAnnotations" -}}
helm.sh/resource-policy: keep
argocd.argoproj.io/sync-options: Prune=false,Delete=false
{{- end -}}

{{/* URL prefix without a trailing slash: "/r", or "" for root */}}
{{- define "gerrit.urlPrefix" -}}
{{- $p := trimAll "/" .Values.gerrit.urlPrefix -}}
{{- if $p -}}/{{ $p }}{{- end -}}
{{- end -}}

{{- define "gerrit.canonicalWebUrl" -}}
{{- printf "%s://%s%s/" .Values.gerrit.webScheme .Values.gerrit.host (include "gerrit.urlPrefix" .) -}}
{{- end -}}

{{- define "gerrit.listenUrl" -}}
{{- printf "%s://*:8080%s/" .Values.gerrit.listenScheme (include "gerrit.urlPrefix" .) -}}
{{- end -}}

{{/* gerrit.host without the HTTP port, followed by the SSH port */}}
{{- define "gerrit.sshAdvertisedAddress" -}}
{{- $host := regexReplaceAll ":[0-9]+$" .Values.gerrit.host "" -}}
{{- default (printf "%s:29418" $host) .Values.ssh.advertisedAddress -}}
{{- end -}}

{{/* container.javaOptions: the options the image needs, a heap of 70%
of the memory limit unless container.heapLimit is set, then the
operator's own, as a list */}}
{{- define "gerrit.javaOptions" -}}
{{- $opts := list
  "-Dflogger.backend_factory=com.google.common.flogger.backend.log4j.Log4jBackendFactory#getInstance"
  "-Dflogger.logging_context=com.google.gerrit.server.logging.LoggingContext#getInstance"
  "-Djava.security.egd=file:/dev/./urandom"
  "--add-opens java.base/java.net=ALL-UNNAMED"
  "--add-opens java.base/java.lang.invoke=ALL-UNNAMED" -}}
{{- if and (not .Values.container.heapLimit) (dig "limits" "memory" "" (default dict .Values.resources)) -}}
{{- $opts = append $opts "-XX:MaxRAMPercentage=70.0" -}}
{{- end -}}
{{- toJson (concat $opts (default list .Values.container.javaOptions)) -}}
{{- end -}}

{{/* One git-config value. Whole numbers print without an exponent;
strings are quoted when git config would otherwise misread them. */}}
{{- define "gerrit.configValue" -}}
{{- if kindIs "float64" . -}}
{{- if eq . (float64 (int64 .)) }}{{ int64 . }}{{ else }}{{ . }}{{ end -}}
{{- else if kindIs "string" . -}}
{{- if regexMatch "(^\\s|\\s$|[#;\"\\\\]|\\n)" . -}}
"{{ . | replace "\\" "\\\\" | replace "\"" "\\\"" | replace "\n" "\\n" }}"
{{- else -}}
{{ . }}
{{- end -}}
{{- else -}}
{{ . }}
{{- end -}}
{{- end -}}

{{/* The key lines of one section: null and "" are left out, a list
repeats the key */}}
{{- define "gerrit.configKeys" -}}
{{- range $key, $value := . }}
{{- if kindIs "slice" $value }}
{{- range $value }}
{{ printf "\t%s = %s" $key (include "gerrit.configValue" .) }}
{{- end }}
{{- else if not (or (kindIs "invalid" $value) (and (kindIs "string" $value) (eq $value ""))) }}
{{ printf "\t%s = %s" $key (include "gerrit.configValue" $value) }}
{{- end }}
{{- end }}
{{- end -}}

{{/* A git-config section from a map: scalar and list keys under
[name], and each map-valued key as a [name "key"] subsection. Renders
nothing when no key has a value. */}}
{{- define "gerrit.configSection" -}}
{{- $name := .name -}}
{{- $plain := dict -}}
{{- $subs := dict -}}
{{- range $key, $value := (default dict .values) -}}
{{- if kindIs "map" $value -}}
{{- $_ := set $subs $key $value -}}
{{- else -}}
{{- $_ := set $plain $key $value -}}
{{- end -}}
{{- end -}}
{{- $keys := include "gerrit.configKeys" $plain -}}
{{- if $keys }}
[{{ $name }}]{{ $keys }}
{{- end }}
{{- range $sub, $values := $subs }}
{{- $keys := include "gerrit.configKeys" $values }}
{{- if $keys }}
[{{ $name }} "{{ $sub | replace "\\" "\\\\" | replace "\"" "\\\"" }}"]{{ $keys }}
{{- end }}
{{- end }}
{{- end -}}

{{/* A whole config file from a map of section name to section map */}}
{{- define "gerrit.configFile" -}}
{{- range $name, $values := . }}
{{- include "gerrit.configSection" (dict "name" $name "values" $values) }}
{{- end }}
{{- end -}}

{{/* Existing value of a Secret key, or a new random one. lookup sees
nothing when the chart is rendered offline, as Argo CD does, so GitOps
installs name pre-created Secrets in secrets.*.existingSecret. */}}
{{- define "gerrit.secretValue" -}}
{{- $existing := lookup "v1" "Secret" .ctx.Release.Namespace .name -}}
{{- if and $existing (index $existing.data .key) -}}
{{- index $existing.data .key -}}
{{- else -}}
{{- randAlphaNum 40 | b64enc -}}
{{- end -}}
{{- end -}}

{{/* Existing value of a Secret key, or an empty one, for keys the
operator fills in after install */}}
{{- define "gerrit.optionalSecretValue" -}}
{{- $existing := lookup "v1" "Secret" .ctx.Release.Namespace .name -}}
{{- if $existing -}}
{{- index (default dict $existing.data) .key | default "" -}}
{{- end -}}
{{- end -}}

{{/* Name of the Secret holding secure.config values */}}
{{- define "gerrit.secureConfigSecretName" -}}
{{- default (printf "%s-secure-config" (include "gerrit.fullname" .)) .Values.secrets.secureConfig.existingSecret -}}
{{- end -}}

{{/* Site directories the image declares as VOLUMEs. Without an
explicit mount the runtime gives each an anonymous volume seeded from
the image, hiding the site on the persistent volume. */}}
{{- define "gerrit.imageVolumeDirs" -}}
cache db etc git index
{{- end -}}

{{/* Site volume mounted at /var/gerrit, plus its image VOLUME dirs */}}
{{- define "gerrit.siteMounts" -}}
- name: site
  mountPath: /var/gerrit
{{- range splitList " " (include "gerrit.imageVolumeDirs" .) }}
- name: site
  mountPath: /var/gerrit/{{ . }}
  subPath: {{ . }}
{{- end }}
{{- end -}}
