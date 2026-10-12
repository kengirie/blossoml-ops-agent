# ltlguard

Rules over an agent's tool calls, written in LTLf (linear temporal logic over finite traces):

- **enforced at runtime** through agent hooks (a call that would make a rule unsatisfiable is denied, with a message that names the rule and lists what can be done instead), and
- **recorded** as JSONL traces for later evaluation.

The same rule file drives both.

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

Templates: `always`, `never`, `eventually`, `not_until`, `response`, `at_most_once`, `at_most` (with `count`).
`on_violation`: `deny` (default) or `log_only` (shadow mode: record what would have been denied).

What a proposition means is up to you: implement a *labeler* (`ltlguard.Labeler`) that maps a tool call
to the propositions true at that step, split into call-time propositions (known before the call) and
result propositions (known after it).

## How it works

```
[dev machine / CI]                            [runtime]
rules.yaml --Spot--> DFA --precompute--> rules.dfa.json --> pure-Python monitor
(ltlguard compile)                                          (no Spot import)
```

- Each rule becomes a minimal complete DFA with, per state: `accepting`, `dead` (no way back to
  acceptance), `permanent` (cannot leave acceptance) and `safe_next` (guard of non-dead moves).
- **PreToolUse** previews one step with the call-time propositions. Result propositions are unknown,
  so a call is denied only if *every* combination of the possible result propositions leads to a dead
  state. State does not change at this point.
- **PostToolUse** commits the step with call-time and result propositions.
- Verdicts have four values: satisfied, violated, tentatively OK, tentatively violated (an open
  obligation). At **Stop**, open obligations send the agent back (up to a limit).
- Calls are serialized; every internal error or timeout is a denial (fail-closed). A tool can call
  `GuardSession.require_cleared()` so it refuses to run if a hook failed open.
- The runtime refuses to load a `rules.dfa.json` whose recorded hash does not match `rules.yaml`.

Layout: `ltlguard/core` (guards, DFA, monitor, session; no SDK), `ltlguard/spec` (YAML, templates),
`ltlguard/trace` (JSONL), `ltlguard/compile` (Spot; the only place that imports it),
`ltlguard/adapters/claude_agent_sdk` (the only place that imports the Claude Agent SDK).

## Commands

```sh
ltlguard compile rules.yaml -o rules.dfa.json   # needs Spot
ltlguard verify  rules.yaml rules.dfa.json      # needs Spot; semantic comparison, for CI
ltlguard check   rules.yaml rules.dfa.json      # no Spot; hash check
```

## Installing Spot (compiler only)

Spot (>= 2.14, with Python bindings) is not on PyPI. It is needed only to compile rules and to run the
differential tests; the runtime never imports it.

- macOS (Homebrew): `brew install spot`. The bindings are built for Homebrew's Python minor version
  (2.16: Python 3.14). Use a venv of that version and add the path with a `.pth` file:

  ```sh
  uv venv --python 3.14 .venv-spot
  echo "$(brew --prefix spot)/lib/python3.14/site-packages" > .venv-spot/lib/python3.14/site-packages/spot-homebrew.pth
  uv pip install --python .venv-spot -e ./ltlguard pytest
  ```

- conda / micromamba: `micromamba create -n spot -c conda-forge 'spot>=2.14' python pyyaml pytest`
  (used in CI).
- Debian/Ubuntu: LRDE's package repository, or build from source.

Tests: `python -m pytest` in `ltlguard/`. Without Spot, the compiler and differential tests are skipped.
The differential tests run each rule's DFA and Spot itself over all short traces and random longer
ones, and check that acceptance and "dead" agree.

## License

Spot is GPL-3.0-or-later. Only `ltlguard.compile` imports it; the runtime does not. The license of
ltlguard as a whole (and whether the compiler should be a separate package) is to be decided before
publishing.
