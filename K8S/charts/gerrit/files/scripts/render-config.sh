#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Write etc/secure.config from the environment, which the chart fills
# from Secrets, at every start. The file is replaced whole, so a value
# removed from the Secret leaves the site too.

set -eu

site=${GERRIT_SITE:?}
secure="$site/etc/secure.config"
tmp="$secure.new"

umask 077
rm -f "$tmp"
: >"$tmp"
git config -f "$tmp" auth.registerEmailPrivateKey \
  "${GERRIT_REGISTER_EMAIL_PRIVATE_KEY:?}"
mv -f "$tmp" "$secure"

echo "Wrote $secure"
