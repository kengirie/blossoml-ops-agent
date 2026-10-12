#!/bin/bash
# Runs on the production host as the deploy user (ubuntu), from CI over ssh.
#   remote-install.sh /opt/blossoml-ops/incoming/blossoml-ops-<version>.tar
# The package holds a conda-pack'ed environment (Python, Spot, dependencies, the app).
# Switches `current` only if everything, including the selfcheck, succeeds.
set -euo pipefail

ROOT=/opt/blossoml-ops
PACKAGE=$1
KEEP=3

[ -f "$PACKAGE" ] || { echo "no such file: $PACKAGE" >&2; exit 1; }
STAGE=$(mktemp -d "$ROOT/incoming/stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
tar -xf "$PACKAGE" -C "$STAGE"
cd "$STAGE"

# 1. checksums
sha256sum --quiet -c SHA256SUMS
VERSION=$(cat VERSION)
case "$VERSION" in (*[!A-Za-z0-9._-]*|'') echo "bad version: $VERSION" >&2; exit 1;; esac
DEST="$ROOT/releases/$VERSION"
[ -e "$DEST" ] && { echo "release $VERSION already exists" >&2; exit 1; }

# 2. unpack the environment into releases/<version>/env and fix its prefixes
mkdir -p "$DEST/env"
tar -xzf env.tar.gz -C "$DEST/env"
# (not -I: conda-unpack imports a helper module that sits next to it)
"$DEST/env/bin/python" "$DEST/env/bin/conda-unpack"
cp VERSION SHA256SUMS "$DEST/"

# The agent user can read but not modify its own code and rules.
chmod -R go-w "$DEST"
chmod -R o+rX "$DEST"

# 3. selfcheck as the agent user: config, rules translated by Spot, tool/rule
#    consistency, built-in tools disabled. No API call, no notification.
#    Secrets are checked at setup (full selfcheck) and at the start of every run.
sudo -n -u blossoml-ops env BLOSSOML_OPS_CONFIG=/etc/blossoml-ops/config.yaml \
  "$DEST/env/bin/python" -I -m blossoml_ops.main selfcheck --skip-secrets

# 4. atomic switch
ln -sfn "releases/$VERSION" "$ROOT/current.tmp"
mv -T "$ROOT/current.tmp" "$ROOT/current"
echo "current -> $VERSION"

# 5. keep the newest $KEEP releases (never the current one)
cd "$ROOT/releases"
CUR=$(basename "$(readlink -f "$ROOT/current")")
ls -1t | { grep -vx "$CUR" || true; } | tail -n +"$KEEP" | while read -r old; do rm -rf -- "$old"; done
rm -f "$PACKAGE"
