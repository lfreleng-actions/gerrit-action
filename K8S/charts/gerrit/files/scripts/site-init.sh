#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Prepare the Gerrit site on the persistent volume. The first boot
# runs "gerrit init"; every start refreshes the binaries and enabled
# plugins from the image and writes gerrit.config from the environment.
# The volume is mounted at GERRIT_SITE so the image's /var/gerrit stays
# visible.

set -eu

site=${GERRIT_SITE:?}
image_bin=/var/gerrit/bin
config="$site/etc/gerrit.config"
catalogue=${GERRIT_CATALOGUE:-/var/gerrit-catalogue}

# Create the directories the pod mounts by subPath before the kubelet
# does: it would create any missing one owned by root
# shellcheck disable=SC2086
(cd "$site" && mkdir -p bin plugins lib ${GERRIT_SITE_DIRS:-})
cp "$image_bin/gerrit.war" "$image_bin/gerrit.sh" "$site/bin/"

# The launcher unpacks into ~/.gerritcodereview, and the gerrit user's
# home is the image's read-only /var/gerrit, so point it at /tmp
if [ ! -f "$config" ]; then
  echo "Initialising a new Gerrit site in $site"
  java -Duser.home=/tmp \
    --add-opens java.base/java.net=ALL-UNNAMED \
    --add-opens java.base/java.lang.invoke=ALL-UNNAMED \
    -jar "$site/bin/gerrit.war" init --batch --no-auto-start -d "$site"
fi

# The site's plugins and libraries are exactly the ones named: clear
# them, then copy each from the image catalogue. An absent catalogue
# is fine while nothing is named.
rm -f "$site"/plugins/*.jar "$site"/lib/*.jar

copy_jars() {
  kind=$1
  dest=$2
  shift 2
  for name in "$@"; do
    src="$catalogue/$kind/$name.jar"
    if [ ! -f "$src" ]; then
      echo "ERROR: $src is not in the image catalogue" >&2
      exit 1
    fi
    cp "$src" "$dest/"
  done
}

# The lists are space-separated jar names: split them, never glob
set -f
# shellcheck disable=SC2086
copy_jars plugins "$site/plugins" ${GERRIT_PLUGINS:-}
# shellcheck disable=SC2086
copy_jars lib "$site/lib" ${GERRIT_LIBS:-}
set +f

cfg() {
  git config -f "$config" "$@"
}

cfg gerrit.canonicalWebUrl "${GERRIT_CANONICAL_WEB_URL:?}"
cfg httpd.listenUrl "${GERRIT_LISTEN_URL:?}"
cfg sshd.listenAddress "${GERRIT_SSHD_LISTEN_ADDRESS:?}"
cfg sshd.advertisedAddress "${GERRIT_SSHD_ADVERTISED_ADDRESS:?}"
cfg auth.type "${GERRIT_AUTH_TYPE:?}"
cfg container.user gerrit

# git config exits 5 when there is nothing to unset
cfg --unset-all container.javaOptions || [ "$?" -eq 5 ]
for opt in \
  "-Dflogger.backend_factory=com.google.common.flogger.backend.log4j.Log4jBackendFactory#getInstance" \
  "-Dflogger.logging_context=com.google.gerrit.server.logging.LoggingContext#getInstance" \
  "-Djava.security.egd=file:/dev/./urandom" \
  "--add-opens java.base/java.net=ALL-UNNAMED" \
  "--add-opens java.base/java.lang.invoke=ALL-UNNAMED" \
  "${GERRIT_JAVA_OPTIONS:-}"; do
  if [ -n "$opt" ]; then
    cfg --add container.javaOptions "$opt"
  fi
done

echo "Gerrit site in $site is ready"
