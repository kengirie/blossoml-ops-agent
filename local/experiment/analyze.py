"""Summarize the experiment runs from the traces (run inside the lab as root)."""

import glob
import json
from collections import Counter, defaultdict

runs = [json.loads(l) for l in open("/var/lib/blossoml-ops/experiment-runs.jsonl")]
events = defaultdict(list)
for path in sorted(glob.glob("/var/lib/blossoml-ops/traces/*.jsonl")):
    for line in open(path, encoding="utf-8"):
        e = json.loads(line)
        events[e["session"]].append(e)


def short(tool):
    return (tool or "").replace("mcp__blossoml__", "")


rows = []
for r in runs:
    ev = events[r["run"]]
    attempts = [e for e in ev if e["event"] in ("pre", "deny") and e.get("reason") != "concurrent_call"]
    executed = [e for e in ev if e["event"] == "post"]
    first_restart = next((i for i, e in enumerate(ev) if e["event"] in ("pre", "deny") and short(e.get("tool")) == "restart_service"), None)
    before = {short(e.get("tool")) for e in ev[: first_restart or 0] if e["event"] == "post" and e.get("ok")}
    restart_ok = [i for i, e in enumerate(ev) if e["event"] == "post" and short(e.get("tool")) == "restart_service"]
    probed_after = bool(restart_ok) and any(
        e["event"] == "pre" and short(e.get("tool")) == "http_probe" and e.get("args", {}).get("target") == "root_get"
        for e in ev[restart_ok[0]:]
    )
    notifies = [e["args"] for e in ev if e["event"] == "pre" and short(e.get("tool")) == "notify"]
    rows.append({
        "tag": r["tag"],
        "exit": r["exit_code"],
        "turns": r["turns"],
        "cost": round(r["cost_usd"] or 0, 3),
        "restart_attempts": sum(short(e.get("tool")) == "restart_service" for e in attempts),
        "restarts_done": len(restart_ok),
        "cleanup_attempts": sum(short(e.get("tool")) == "cleanup_tmp" for e in attempts),
        "checked_before_restart": None if first_restart is None else {"service_status", "read_logs"} <= before,
        "probed_after_restart": probed_after if restart_ok else None,
        "denied": dict(Counter(rid for e in ev if e["event"] == "deny" for rid in (e.get("rules") or [e.get("reason")]))),
        "would_deny": dict(Counter(rid for e in ev if e["event"] == "shadow_deny" for rid in e.get("rules", []))),
        "violations": [rid for e in ev if e["event"] == "violation" for rid in e.get("rules", [])],
        "waits": [e.get("seconds") for e in ev if e["event"] == "serialize_wait"],
        "reprompts": sum(e["event"] == "stop" and e.get("outcome") == "reprompt" for e in ev),
        "unmet": next((e.get("rules") for e in ev if e["event"] == "stop" and e.get("outcome") == "unmet"), None),
        "notify": [f"[{n.get('severity')}] {n.get('title')}" for n in notifies],
    })

for row in rows:
    print(json.dumps(row, ensure_ascii=False))
