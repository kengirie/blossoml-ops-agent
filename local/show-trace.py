"""Print one agent run from the trace files in a readable form.

Runs inside the lab container (it is piped in by lab.sh):
    show-trace.py [run-id]      # default: the latest run
"""

import glob
import json
import sys

events = []
for path in sorted(glob.glob("/var/lib/blossoml-ops/traces/*.jsonl")):
    with open(path, encoding="utf-8") as f:
        events += [json.loads(line) for line in f if line.strip()]
if not events:
    sys.exit("no traces yet")

runs = list(dict.fromkeys(e["session"] for e in events))
run = sys.argv[1] if len(sys.argv) > 1 else runs[-1]
events = [e for e in events if e["session"] == run]
print(f"run {run}  ({len(runs)} runs in total: {', '.join(runs[-5:])})\n")


def short(value, n=160):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= n else text[: n - 1] + "…"


for e in events:
    t = e["ts"][11:19]
    kind = e["event"]
    tool = e.get("tool", "").replace("mcp__blossoml__", "")
    if kind == "pre":
        print(f"{t}  → {tool} {short(e.get('args', {}))}")
    elif kind == "post":
        res = e.get("result_summary")
        print(f"{t}  ← {tool} {'ok' if e.get('ok') else 'FAILED'}  {short(res) if res is not None else ''}")
    elif kind == "deny":
        print(f"{t}  ✗ DENIED {tool} rules={e.get('rules')} reason={e.get('reason', '')}")
    elif kind == "stop":
        print(f"{t}  ■ stop: {e.get('outcome')} {e.get('rules', '')}")
    elif kind == "guard_violation":
        print(f"{t}  ✗ OUTPUT DISCARDED {tool} kind={e.get('kind')}")
    elif kind == "result":
        print(f"{t}  = result: {e.get('subtype')} turns={e.get('num_turns')} cost=${e.get('cost_usd')}")
    elif kind == "harness_alert":
        print(f"{t}  ! harness alert sent={e.get('delivered')} ({e.get('count')} items)")
    elif kind == "end":
        print(f"{t}  = exit code {e.get('exit_code')}")
        print("\nfinal rule states:")
        for rid, st in (e.get("monitor") or {}).items():
            print(f"  {rid:<4} {st['verdict']:<20} residual: {st['residual']}")
    elif kind in ("start", "rules"):
        print(f"{t}  {kind}: {short({k: v for k, v in e.items() if k not in ('ts', 'session', 'seq', 'event')})}")
    else:
        print(f"{t}  {kind} {short({k: v for k, v in e.items() if k not in ('ts', 'session', 'seq', 'event', 'monitor')})}")

notifies = [e for e in events if e["event"] == "pre" and e.get("tool", "").endswith("notify")]
for e in notifies:
    a = e.get("args", {})
    print(f"\n--- notify [{a.get('severity')}] {a.get('title')}\n{a.get('body')}")
