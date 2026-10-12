# blossoml-ops-agent

The implementation of `blossoml-ops-design.md`:

- `ltlguard/`: LTLf rules over agent tool calls (Spot compiler, pure-Python runtime, Claude Agent SDK adapter, JSONL traces). See `ltlguard/README.md`.
- `blossoml_ops/`: the operations agent for the blossoML production server (8 read-only tools, 3 phase-2 write tools, output guard, NIP-17 notifications, daily / postdeploy / manual modes).
- `blossoml_ops/deploy/`: production setup (users and ACLs, proxy and firewall, systemd, sudoers, root-owned scripts, `remote-install.sh`). See `blossoml_ops/deploy/README.md`.
- `blossoml-patches/`: the patch for blossoML itself (blob files created with mode 0640).
- `.github/workflows/deploy.yml`: tests → Spot rule verification → wheelhouse → deploy.

## Scope decisions

- **Human approval is out of scope.** There is no `request_approval` tool, no rule R3, and no `rollback_binary` tool (a rollback without approval would defeat R3's purpose). The agent proposes a rollback in its report; the operator performs it. R4 checks only restarts.
- **R1 formula corrected.** The design's `¬restart W (status_checked ∧ logs_read)` needs both checks at the same step, which never happens because each step is one tool call. It is now `(¬restart W status_checked) ∧ (¬restart W logs_read)`.
- **R9 added**: at most 5 warning/critical notifications (design 6.4 asks for it to be expressed in LTL too).
- Phase 2 (write tools, R1/R2/R4/R5) is implemented but disabled by default (`phase2.enabled: false`).
- Phase 3 (evaluation mode) is not implemented; traces already contain what it needs.

## Development

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv -e ./ltlguard -e './blossoml_ops[test]' --config-settings editable_mode=compat
(cd ltlguard && ../.venv/bin/python -m pytest -q)
(cd blossoml_ops && ../.venv/bin/python -m pytest -q)

# Rules: edit blossoml_ops/blossoml_ops/rules.yaml, then (needs Spot, see ltlguard/README.md)
.venv-spot/bin/python -m ltlguard compile blossoml_ops/blossoml_ops/rules.yaml
```

`editable_mode=compat` matters: the project directories `ltlguard/` and `blossoml_ops/` would otherwise
shadow the packages as namespace packages when running from the repository root.
