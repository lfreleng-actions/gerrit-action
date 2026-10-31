{{- /*
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation

etc/gerrit.config, rendered with tpl into the <fullname>-config
ConfigMap. Keys the chart derives win over the same key in values.
*/ -}}
{{- $v := .Values -}}
{{- $derived := dict
  "gerrit" (dict
    "basePath" "git"
    "canonicalWebUrl" (include "gerrit.canonicalWebUrl" .)
    "instanceId" $v.gerrit.instanceId
    "serverId" $v.gerrit.serverId)
  "httpd" (dict "listenUrl" (include "gerrit.listenUrl" .))
  "sshd" (dict "advertisedAddress" (include "gerrit.sshAdvertisedAddress" .))
  "cache" (dict "directory" "cache")
  "container" (dict "javaOptions" (include "gerrit.javaOptions" . | fromJsonArray)) -}}
{{- $sections := list "gerrit" "httpd" "sshd" "auth" "sendemail" "container" "cache" "change" "receive" "download" "index" "gc" "plugins" "user" "trackingid" "accounts" "suggest" -}}
{{- range $sections }}
{{- $values := deepCopy (default dict (index $v .)) -}}
{{- if eq . "gerrit" }}{{ $values = dict }}{{ end -}}
{{- $values = merge (deepCopy (default dict (index $derived .))) $values -}}
{{- include "gerrit.configSection" (dict "name" . "values" $values) }}
{{- end }}
{{- with $v.gerrit.extraConfig }}
{{ trimSuffix "\n" . }}
{{- end }}
