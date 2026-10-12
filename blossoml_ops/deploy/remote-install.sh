#!/bin/bash
# Runs on the production host as the deploy user (ubuntu), from CI over ssh.
#   remote-install.sh /opt/blossoml-ops/incoming/blossoml-ops-<version>.tar.gz
# Switches `current` only if everything, including the selfcheck, succeeds.
set -euo pipefail

ROOT=/opt/blossoml-ops
PYTHON=${PYTHON:-/usr/bin/python3}
TARBALL=$1
KEEP=3

[ -f "$TARBALL" ] || { echo "no such file: $TARBALL" >&2; exit 1; }
STAGE=$(mktemp -d "$ROOT/incoming/stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$TARBALL" -C "$STAGE"
cd "$STAGE"

# 1. checksums
sha256sum --quiet -c SHA256SUMS
VERSION=$(cat VERSION)
case "$VERSION" in (*[!A-Za-z0-9._-]*|'') echo "bad version: $VERSION" >&2; exit 1;; esac
DEST="$ROOT/releases/$VERSION"
[ -e "$DEST" ] && { echo "release $VERSION already exists" >&2; exit 1; }

# 2. unpack into releases/<version>
mkdir -p "$DEST"
cp -a app wheelhouse VERSION SHA256SUMS "$DEST/"

# 3. venv from the wheelhouse only (no network, no build tools)
"$PYTHON" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --no-index --find-links "$DEST/wheelhouse" --require-hashes -r "$DEST/app/requirements.lock"
"$DEST/venv/bin/pip" install --quiet --no-index --no-deps "$DEST"/wheelhouse/ltlguard-*.whl "$DEST"/wheelhouse/blossoml_ops-*.whl
rm -rf "$DEST/wheelhouse"

# The agent user can read but not modify its own code, rules or DFA.
chmod -R go-w "$DEST"
chmod -R o+rX "$DEST"

# 4. selfcheck as the agent user: config, rule hash, tool/rule consistency,
#    built-in tools disabled. No API call, no notification. Secrets are checked
#    at setup (full selfcheck) and at the start of every run.
sudo -n -u blossoml-ops env BLOSSOML_OPS_CONFIG=/etc/blossoml-ops/config.yaml \
  "$DEST/venv/bin/python" -I -m blossoml_ops.main selfcheck --skip-secrets

# 5. atomic switch
ln -sfn "releases/$VERSION" "$ROOT/current.tmp"
mv -T "$ROOT/current.tmp" "$ROOT/current"
echo "current -> $VERSION"

# 6. keep the newest $KEEP releases (never the current one)
cd "$ROOT/releases"
CUR=$(basename "$(readlink -f "$ROOT/current")")
ls -1t | grep -vx "$CUR" | tail -n +"$KEEP" | while read -r old; do rm -rf -- "$old"; done
rm -f "$TARBALL"
