#!/bin/bash
# S3: the instruction itself asks for rule violations (local/experiment/adversarial.txt).
# One run with the rules enforced, one in shadow mode. Then the traces.
# Extra arguments are passed to exp_agent.py (e.g. --plain-prompt).
set -euo pipefail
cd "$(dirname "$0")/.."
EXTRA=("$@")
PREFIX=S3; [ "${#EXTRA[@]}" -gt 0 ] && PREFIX=S4
X() { docker compose exec -T lab "$@"; }
agent() {
  X systemd-run --wait --pipe --collect --quiet --uid=blossoml-ops --gid=blossoml-ops \
    -p SupplementaryGroups=systemd-journal -p MemoryMax=350M -p CPUQuota=50% -p Nice=10 -p OOMScoreAdjust=800 \
    -p ProtectSystem=strict -p ProtectHome=read-only -p PrivateTmp=yes -p ReadWritePaths=/var/lib/blossoml-ops \
    -p WorkingDirectory=/var/lib/blossoml-ops/work -E HOME=/var/lib/blossoml-ops \
    /opt/blossoml-ops/current/env/bin/python -I /opt/lab/experiment/exp_agent.py \
    --instruction-file /opt/lab/experiment/adversarial.txt ${EXTRA[@]+"${EXTRA[@]}"} --tag "$@" 2>/dev/null | tail -1
}
for cond in enforce shadow; do
  X bash /opt/lab/experiment/scenario.sh reset >/dev/null
  flag=""; [ "$cond" = shadow ] && flag="--shadow"
  echo "== $PREFIX $cond"
  run=$(agent "$PREFIX-$cond" $flag)
  echo "$run"
  ./lab.sh trace "$(echo "$run" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run"])')"
done
X bash /opt/lab/experiment/scenario.sh reset >/dev/null
X python3 /opt/lab/experiment/analyze.py | grep "\"$PREFIX-"
