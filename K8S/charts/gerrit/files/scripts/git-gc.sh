#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Run "git gc" over every repository under the site's git directory,
# for the chart's gc CronJob. The script's arguments are the git gc
# options; gc.aggressive in etc/gerrit.config adds "--aggressive", and
# etc/jgit.config is read as the system git config, so the [gc],
# [pack] and [core] settings Gerrit's own gc uses apply here too.
# gc.autoDetach is off, so "git gc --auto" finishes, and reports its
# status, before the script moves on.
# A per-repository lock skips a repository another run still holds.
# A repository that fails does not stop the others; the script exits
# non-zero after the last one.

set -eu

site=${GERRIT_SITE:?}
store="$site/git"

if ! command -v flock >/dev/null 2>&1; then
  echo "ERROR: flock is not installed" >&2
  exit 1
fi
if [ ! -d "$store" ]; then
  echo "ERROR: no git directory at $store" >&2
  exit 1
fi

if [ -f "$site/etc/jgit.config" ]; then
  GIT_CONFIG_SYSTEM="$site/etc/jgit.config"
  export GIT_CONFIG_SYSTEM
fi
if [ -f "$site/etc/gerrit.config" ] &&
  [ "$(git config -f "$site/etc/gerrit.config" --type=bool --default=false gc.aggressive)" = true ]; then
  set -- "$@" --aggressive
fi

list=$(mktemp)
trap 'rm -f "$list"' EXIT
find "$store" -type d -name '*.git' -prune -print | sort > "$list"

total=0
failed=0
while IFS= read -r repo; do
  total=$((total + 1))
  echo "gc $repo"
  rc=0
  (
    if ! flock -n 9; then
      echo "skip $repo: locked by another gc"
      exit 0
    fi
    git -c gc.autoDetach=false --git-dir="$repo" gc "$@"
  ) 9>"$repo/chart-gc.lock" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: git gc failed with $rc in $repo" >&2
    failed=$((failed + 1))
  fi
done < "$list"

echo "git gc ran over $total repositories, $failed failed"
[ "$failed" -eq 0 ]
