# blossoml-ops-agent

The implementation of `blossoml-ops-design.md`:

- `ltlguard/`: LTLf rules over agent tool calls. Spot parses and translates the rules and steps the automata at runtime; ltlguard adds the hook adapter for the Claude Agent SDK and JSONL traces. See `ltlguard/README.md`.
- `blossoml_ops/`: the operations agent for the blossoML production server (8 read-only tools, 3 phase-2 write tools, output guard, NIP-17 notifications, daily / postdeploy / manual modes).
- `blossoml_ops/deploy/`: production setup (users and ACLs, proxy and firewall, systemd, sudoers, root-owned scripts, `remote-install.sh`). See `blossoml_ops/deploy/README.md`.
- `blossoml-patches/`: the patch for blossoML itself (blob files created with mode 0640).
- `.github/workflows/deploy.yml`: tests (in a conda env with Spot) → a conda-pack'ed runtime env built on the production architecture → deploy.

## Scope decisions

- **Human approval is out of scope.** There is no `request_approval` tool, no rule R3, and no `rollback_binary` tool (a rollback without approval would defeat R3's purpose). The agent proposes a rollback in its report; the operator performs it. R4 checks only restarts.
- **R1 formula corrected.** The design's `¬restart W (status_checked ∧ logs_read)` needs both checks at the same step, which never happens because each step is one tool call. It is now `(¬restart W status_checked) ∧ (¬restart W logs_read)`.
- **R9 added**: at most 5 warning/critical notifications (design 6.4 asks for it to be expressed in LTL too).
- Phase 2 (write tools, R1/R2/R4/R5) is implemented but disabled by default (`phase2.enabled: false`).
- Phase 3 (evaluation mode) is not implemented; traces already contain what it needs.
- **Spot is used at runtime too** (a change from design 5.3, which kept Spot out of production). Rules are translated from `rules.yaml` at every start (about 60 ms, about 20 MB of memory), so there is no precompiled `rules.dfa.json`, no hand-written guard parser and no hand-written automaton algorithms. Production gets Spot through the conda-forge environment shipped by CI.

## Development

```sh
micromamba create -p ./.env -c conda-forge python=3.12 'spot>=2.14' pip
./.env/bin/python -m pip install -e ./ltlguard -e './blossoml_ops[test]' --config-settings editable_mode=compat
(cd ltlguard && ../.env/bin/python -m pytest -q)
(cd blossoml_ops && ../.env/bin/python -m pytest -q)

# After editing blossoml_ops/blossoml_ops/rules.yaml
./.env/bin/python -m ltlguard check blossoml_ops/blossoml_ops/rules.yaml
./.env/bin/python -m ltlguard show blossoml_ops/blossoml_ops/rules.yaml --rule R1
```

`editable_mode=compat` matters: the project directories `ltlguard/` and `blossoml_ops/` would otherwise
shadow the packages as namespace packages when running from the repository root.
