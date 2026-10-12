#!/bin/bash
# Shortcuts for the local lab. Run from anywhere: local/lab.sh <command>
set -euo pipefail
cd "$(dirname "$0")"
X() { docker compose exec "$@"; }

case "${1:-help}" in
  run)        # run the agent: daily (default) | postdeploy
    mode=${2:-daily}
    echo "running blossoml-ops@$mode (this waits until it finishes)..."
    X lab systemctl start "blossoml-ops@$mode" || true
    X lab journalctl -u "blossoml-ops@$mode" -o cat --no-pager -n 12 ;;
  manual)     # run the agent with an instruction
    X lab /opt/blossoml-ops/bin/run-manual "${2:?usage: lab.sh manual \"instruction\"}" ;;
  result)     # summary of the last run of a mode: exit code, turns, cost
    X lab journalctl -u "blossoml-ops@${2:-daily}" -o cat --no-pager -n 30 ;;
  trace)      # the last run (or a given run id), step by step, with the notification text
    X -T lab python3 - "${@:2}" < show-trace.py ;;
  follow)     # live: agent and blossoML logs while a run is going (Ctrl-C to stop)
    X lab journalctl -f -o short-iso -u 'blossoml-ops@*' -u blossoml -u tinyproxy ;;
  blossoml)   # blossoML's own log (the input of read_logs)
    X lab journalctl -u blossoml -o cat --no-pager -n "${2:-50}" ;;
  init)       # lab setup log (seed data, bot npub)
    X lab journalctl -u lab-init -o cat --no-pager | grep '\[lab-init\]' ;;
  raw)        # the raw JSONL trace files
    X lab bash -c 'ls -l /var/lib/blossoml-ops/traces/ && tail -n "${0}" /var/lib/blossoml-ops/traces/*.jsonl' "${2:-20}" ;;
  undelivered) # notifications that reached no relay
    X lab bash -c 'cat /var/lib/blossoml-ops/undelivered.jsonl 2>/dev/null || echo none' ;;
  shell)      # a root shell in the lab
    X lab bash ;;
  *)
    sed -n 's/^  \([a-z]*\)) *# \(.*\)/  \1\t\2/p' "$0" | expand -t 14 ;;
esac
