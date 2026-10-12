#!/bin/bash
# Boot time: secrets from the host, the firewall, ACL on the DB, and seed data.
set -uo pipefail
PY=/opt/blossoml-ops/current/env/bin/python
HOST_SECRETS=/run/host-secrets
CFG=/etc/blossoml-ops/config.yaml
log() { echo "[lab-init] $*"; }

# --- secrets (deploy/README.md 6) ---------------------------------------------
if [ ! -s "$HOST_SECRETS/nostr_bot_nsec" ]; then
  log "no bot key in local/secrets: generating one (kept on the host for the next runs)"
  "$PY" -I -m blossoml_ops.main keygen --out "$HOST_SECRETS/nostr_bot_nsec"
fi
install -o blossoml-ops -g blossoml-ops -m 0400 "$HOST_SECRETS/nostr_bot_nsec" /etc/blossoml-ops/nostr_bot_nsec
if [ -s "$HOST_SECRETS/anthropic_api_key" ]; then
  install -o blossoml-ops -g blossoml-ops -m 0400 "$HOST_SECRETS/anthropic_api_key" /etc/blossoml-ops/anthropic_api_key
else
  log "WARNING: local/secrets/anthropic_api_key is missing; agent runs will fail until it is added (then restart lab-init)"
fi

# --- firewall (deploy/README.md 3) ----------------------------------------------
if nft -f /etc/nftables.d/blossoml-ops.nft; then
  log "nftables rules for blossoml-ops loaded"
else
  log "WARNING: could not load nftables rules (I4 is not enforced in this container)"
fi

# --- wait for blossoML ------------------------------------------------------------
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null http://localhost:8082/ && break
  sleep 1
done
# ACLs (deploy/README.md 2). provision.sh sets them too, but Docker image layers
# do not keep POSIX ACLs, so they are applied again here.
setfacl -m u:blossoml-ops:x  /home/ubuntu /home/ubuntu/blossoML
setfacl -m u:blossoml-ops:rx /home/ubuntu/blossoML/data /home/ubuntu/blossoML/db
setfacl -m u:blossoml-ops:r  /home/ubuntu/blossoML/main.exe /home/ubuntu/blossoML/main.exe.backup /home/ubuntu/blossoML/db/blossom.db

# --- seed data, once per container -------------------------------------------------
if [ ! -e /var/lib/lab-seeded ]; then
  SHA=$(sudo -u ubuntu "$PY" -I /opt/lab/seed.py) || { log "seeding failed"; exit 1; }
  sed -i "s/^test_blob_sha256: .*/test_blob_sha256: $SHA/" "$CFG"
  touch /var/lib/lab-seeded
  log "seeded; test blob $SHA"
fi

log "bot npub: $(sudo -u blossoml-ops "$PY" -I -c '
from blossoml_ops.notify.keys import bech32_encode, parse_secret_key, xonly_pubkey
s = parse_secret_key(open("/etc/blossoml-ops/nostr_bot_nsec").read())
print(bech32_encode("npub", bytes.fromhex(xonly_pubkey(s))))')"
sudo -u blossoml-ops env BLOSSOML_OPS_CONFIG=$CFG "$PY" -I -m blossoml_ops.main selfcheck && log "ready"
