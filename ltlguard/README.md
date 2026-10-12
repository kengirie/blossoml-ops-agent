# ltlguard

Rules over an agent's tool calls, written in LTLf (linear temporal logic over finite traces):

- **enforced at runtime** through agent hooks (a call that would make a rule unsatisfiable is denied, with a message that names the rule and lists what can be done instead), and
- **recorded** as JSONL traces for later evaluation.

The same rule file drives both. [Spot](https://spot.lre.epita.fr/) does the logic: it parses the formulas,
translates them to automata, and decides satisfiability; ltlguard only wires the automata to tool calls.

```yaml
version: 1
rules:
  - id: R1
    name: check before restart
    description: Call service_status and read_logs before restart_service.
    formula: "(!restart W status_checked) & (!restart W logs_read)"
  - id: R4
    name: probe after restart
    template: response          # G(restart -> F probe_ok)
    trigger: restart
    response: probe_ok
```

Templates (built with Spot's formula constructors; arguments are Boolean formulas parsed by Spot):
`always`, `never`, `eventually`, `not_until`, `response`, `at_most_once`, `at_most` (with `count`).
`on_violation`: `deny` (default) or `log_only` (shadow mode: record what would have been denied).

What a proposition means is up to you: implement a *labeler* (`ltlguard.Labeler`) that maps a tool call
to the propositions true at that step, split into call-time propositions (known before the call) and
result propositions (known after it).

## How it works

At startup each rule goes through `spot.parse_infix_psl` → `spot.ltlf_to_mtdfa` → `spot.minimize_mtdfa`
→ `mtdfa.as_twa`. The result is a deterministic automaton whose states are labeled with the residual
LTLf formula (what the rest of the run must satisfy) and whose transitions carry a final mark.

- A step intersects each transition's BDD guard with the BDD cube of the true propositions.
- Accepted if the run ends now: the final mark of the last transition.
- Definitively violated ("dead"): not accepted now and the residual formula is unsatisfiable
  (`ltlf_to_mtdfa(residual).is_empty()`).
- Definitively satisfied: accepted now and the residual formula is valid (`ltlf_to_mtdfa(!residual).is_empty()`).

Monitoring:

- **PreToolUse** previews one step with the call-time propositions. Result propositions are unknown,
  so a call is denied only if *every* combination of the possible result propositions leads to a dead
  state. State does not change at this point.
- **PostToolUse** commits the step with call-time and result propositions.
- Verdicts have four values: satisfied, violated, tentatively OK, tentatively violated (an open
  obligation). At **Stop**, open obligations send the agent back (up to a limit).
- Calls are serialized; every internal error or timeout is a denial (fail-closed). A tool can call
  `GuardSession.require_cleared()` so it refuses to run if a hook failed open.
- Traces record each rule's residual formula after every step.

Spot defines LTLf on non-empty traces; for the state before any event, weak operators (`G`, `W`, `R`,
weak `X`) hold and strong ones (`F`, `U`, `M`, `X[!]`) do not.

Layout: `ltlguard/spec` (YAML, templates), `ltlguard/core` (automata, monitor, session; no SDK),
`ltlguard/trace` (JSONL), `ltlguard/adapters/claude_agent_sdk` (the only place that imports the
Claude Agent SDK).

## Commands

```sh
ltlguard check rules.yaml                          # parse and translate every rule; summary
ltlguard show  rules.yaml [--rule R1] [--format hoa|dot]   # Spot's automata, for review
```

## Installing Spot

Spot (>= 2.14, with Python bindings) is not on PyPI. Use conda-forge (Linux x86_64/aarch64, macOS arm64):

```sh
micromamba create -p ./.env -c conda-forge python=3.12 'spot>=2.14' pyyaml pytest pytest-asyncio
./.env/bin/python -m pip install -e ./ltlguard
```

Tests: `python -m pytest` in `ltlguard/`. The differential tests run each rule's monitor over all short
traces and random longer ones and compare acceptance and "dead" with Spot's own judgment.

## License

Spot is GPL-3.0-or-later, and ltlguard imports it at runtime. Take this into account when choosing
ltlguard's license before publishing it as a package.
