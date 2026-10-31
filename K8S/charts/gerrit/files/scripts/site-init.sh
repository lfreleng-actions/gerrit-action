#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Prepare the Gerrit site on the persistent volume. Every start
# replaces etc/gerrit.config, jgit.config and notedb.config with the
# files the chart rendered, and refreshes the binaries and enabled
# plugins from the image; the first boot then runs "gerrit init" on
# the rendered configuration. The volume is mounted at GERRIT_SITE so
# the image's /var/gerrit stays visible.

set -eu

site=${GERRIT_SITE:?}
image_bin=/var/gerrit/bin
config="$site/etc/gerrit.config"
rendered=${GERRIT_RENDERED_CONFIG:?}
catalogue=${GERRIT_CATALOGUE:-/var/gerrit-catalogue}

# Create the directories the pod mounts by subPath before the kubelet
# does: it would create any missing one owned by root
# shellcheck disable=SC2086
(cd "$site" && mkdir -p bin plugins lib ${GERRIT_SITE_DIRS:-})
cp "$image_bin/gerrit.war" "$image_bin/gerrit.sh" "$site/bin/"

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

# Print a key from a config file, or nothing when the key is absent.
# Any other failure, such as an unreadable or malformed file, stops
# the script: reading it as absent could discard the site's identity.
get_key() {
  if [ ! -r "$1" ]; then
    echo "ERROR: cannot read $1" >&2
    exit 1
  fi
  rc=0
  git config -f "$1" --get "$2" || rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "ERROR: cannot read $2 from $1" >&2
    exit 1
  fi
}

# gerrit.serverId is site identity: Gerrit refuses changes stamped
# with another ID, so no render may change it. An empty value keeps
# the site's own ID.
want_id=$(get_key "$rendered/gerrit.config" gerrit.serverId)
site_id() {
  if [ -e "$config" ]; then
    get_key "$config" gerrit.serverId
  fi
}
have_id=$(site_id)
if [ -n "$want_id" ] && [ -n "$have_id" ] && [ "$want_id" != "$have_id" ]; then
  echo "ERROR: gerrit.serverId $want_id does not match the site's $have_id" >&2
  exit 1
fi

install_config() {
  cp "$rendered/gerrit.config" "$config.new"
  if [ -z "$want_id" ] && [ -n "$have_id" ]; then
    git config -f "$config.new" gerrit.serverId "$have_id"
  fi
  mv -f "$config.new" "$config"
  for name in jgit.config notedb.config; do
    cp "$rendered/$name" "$site/etc/$name"
  done
}

install_config

# A site without its All-Projects repository, named by
# gerrit.allProjects, has never been initialised. Init runs on the
# rendered configuration, so a set serverId becomes the site's ID;
# the render is then installed again over whatever init added. The
# launcher unpacks into ~/.gerritcodereview, and the gerrit user's
# home is the image's read-only /var/gerrit, so point it at /tmp.
all_projects=$(get_key "$config" gerrit.allProjects)
if [ ! -d "$site/git/${all_projects:-All-Projects}.git" ]; then
  echo "Initialising a new Gerrit site in $site"
  java -Duser.home=/tmp \
    --add-opens java.base/java.net=ALL-UNNAMED \
    --add-opens java.base/java.lang.invoke=ALL-UNNAMED \
    -jar "$site/bin/gerrit.war" init --batch --no-auto-start -d "$site"
  have_id=$(site_id)
  install_config
fi

echo "Gerrit site in $site is ready"
