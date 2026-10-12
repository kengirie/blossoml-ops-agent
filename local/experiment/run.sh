#!/bin/bash
# Run the LTL experiment from the host: local/experiment/run.sh [repeats]
# Scenarios S1 (broken deploy) and S2 (prompt injection in the log), each with
# the rules enforced and in shadow mode. Results: analyze.py.
set -euo pipefail
cd "$(dirname "$0")/.."
N=${1:-2}
X() { docker compose exec -T lab "$@"; }
agent() {  # tag [--shadow]
  X systemd-run --wait --pipe --collect --quiet --uid=blossoml-ops --gid=blossoml-ops \
    -p SupplementaryGroups=systemd-journal -p MemoryMax=350M -p CPUQuota=50% -p Nice=10 -p OOMScoreAdjust=800 \
    -p ProtectSystem=strict -p ProtectHome=read-only -p PrivateTmp=yes -p ReadWritePaths=/var/lib/blossoml-ops \
    -p WorkingDirectory=/var/lib/blossoml-ops/work -E HOME=/var/lib/blossoml-ops \
    /opt/blossoml-ops/current/env/bin/python -I /opt/lab/experiment/exp_agent.py --tag "$@" 2>/dev/null | tail -1
}
for scenario in S1 S2; do
  for i in $(seq 1 "$N"); do
    for cond in enforce shadow; do
      X bash /opt/lab/experiment/scenario.sh reset >/dev/null
      X bash /opt/lab/experiment/scenario.sh "$scenario" >/dev/null
      flag=""; [ "$cond" = shadow ] && flag="--shadow"
      echo "== $scenario $cond #$i"
      agent "$scenario-$cond-$i" $flag
    done
  done
done
X bash /opt/lab/experiment/scenario.sh reset >/dev/null
X python3 /opt/lab/experiment/analyze.py
