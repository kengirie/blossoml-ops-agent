"""One experiment run of the real agent (run as blossoml-ops inside the lab).

Differences from a normal run, and nothing else:
- phase 2 (write tools) is enabled;
- Nostr publishing is replaced by a fake that accepts everything, so the
  operator does not get a DM per experiment run;
- with --shadow, every rule is switched to on_violation: log_only, so
  violations are recorded but not prevented.

    exp_agent.py --tag S1-enforce-1 [--shadow]
"""

import argparse
import asyncio
import dataclasses
import json
import re
import tempfile
from pathlib import Path

from blossoml_ops import agent
from blossoml_ops.config import load_config, read_secret
from blossoml_ops.notify.keys import parse_secret_key
from blossoml_ops.notify.relay import PublishResult

RUNS_LOG = Path("/var/lib/blossoml-ops/experiment-runs.jsonl")


async def fake_publish(relays, event, **kw):
    return [PublishResult(r, True, "") for r in relays]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tag", required=True)
    p.add_argument("--shadow", action="store_true")
    args = p.parse_args()

    cfg = dataclasses.replace(load_config("/etc/blossoml-ops/config.yaml"), phase2_enabled=True)
    if args.shadow:
        text = agent.RULES_YAML.read_text(encoding="utf-8")
        text = re.sub(r"on_violation: deny", "on_violation: log_only", text)
        shadow = Path(tempfile.mkdtemp()) / "rules.yaml"
        shadow.write_text(text, encoding="utf-8")
        agent.RULES_YAML = shadow
        # check_rules requires R0/R6/R5'/R8 to be enforced; relax it for the shadow condition only.
        agent.check_rules = lambda ruleset, labeler: []

    report = asyncio.run(agent.run_agent(
        cfg, "daily",
        api_key=read_secret(cfg.anthropic_api_key_file),
        nostr_secret=parse_secret_key(read_secret(cfg.nostr_nsec_file)),
        publisher=fake_publish,
    ))
    rec = {"tag": args.tag, "shadow": args.shadow, "run": report.run_id, "exit_code": report.exit_code,
           "turns": report.num_turns, "cost_usd": report.cost_usd}
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))


if __name__ == "__main__":
    main()
