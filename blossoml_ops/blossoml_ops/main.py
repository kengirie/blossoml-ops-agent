"""Entry point.

    python -m blossoml_ops.main daily
    python -m blossoml_ops.main postdeploy
    python -m blossoml_ops.main manual --instruction "..."   (or the instruction on stdin)
    python -m blossoml_ops.main selfcheck [--skip-secrets]    (no API calls, no notifications)
    python -m blossoml_ops.main keygen --out /etc/blossoml-ops/nostr_bot_nsec
    python -m blossoml_ops.main publish-profile               (kind 0 for the bot)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from blossoml_ops.config import DEFAULT_CONFIG_PATH, ConfigError, load_config, read_secret


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_run(args) -> int:
    from blossoml_ops.agent import EXIT_CONFIG, run_agent
    from blossoml_ops.notify.keys import parse_secret_key

    try:
        cfg = load_config(args.config)
        api_key = read_secret(cfg.anthropic_api_key_file)
        secret = parse_secret_key(read_secret(cfg.nostr_nsec_file))
    except (ConfigError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    instruction = None
    if args.mode == "manual":
        instruction = args.instruction if args.instruction else sys.stdin.read()
    report = asyncio.run(run_agent(cfg, args.mode, instruction, api_key=api_key, nostr_secret=secret))
    _print({"run": report.run_id, "exit_code": report.exit_code, "result": report.result_subtype,
            "turns": report.num_turns, "cost_usd": report.cost_usd, "notes": report.notes})
    return report.exit_code


def selfcheck(config_path: str, skip_secrets: bool = False) -> list[str]:
    """Checks that need no network: configuration, rules, tool/rule consistency,
    SDK options (built-in tools disabled etc.)."""
    from ltlguard.core.automaton import RulesError, load_rules
    from ltlguard.core.monitor import Monitor
    from ltlguard.core.session import GuardSession
    from ltlguard.adapters.claude_agent_sdk import ClaudeAgentSdkAdapter

    from blossoml_ops import agent
    from blossoml_ops.labeling import OpsLabeler
    from blossoml_ops.tools import MetricsHistory, ToolContext, allowed_tool_names, build_sdk_server, build_specs, representative_args

    problems: list[str] = []
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        return [f"config: {e}"]
    try:
        ruleset = load_rules(agent.RULES_YAML)
    except RulesError as e:
        return [f"rules: {e}"]

    specs = build_specs(cfg.phase2_enabled)
    allowed = allowed_tool_names(specs)
    labeler = OpsLabeler([s.name for s in specs], representative_args(specs))
    problems += agent.check_rules(ruleset, labeler)

    class _NullTrace:
        session = "selfcheck"

        def write(self, *a, **k):
            return {}

    session = GuardSession(Monitor(ruleset), labeler, _NullTrace())  # type: ignore[arg-type]
    adapter = ClaudeAgentSdkAdapter(session)
    ctx = ToolContext(config=cfg, session=session, trace=_NullTrace(), notifier=None,
                      history=MetricsHistory(cfg.state_dir / "metrics.json"), alert=lambda *a: None)
    server = build_sdk_server(specs, ctx)
    options = agent.build_options(cfg, system_prompt=agent.build_system_prompt(cfg), allowed=allowed,
                                  server=server, hooks=adapter.hooks(), env=agent.build_env(cfg, None))
    problems += agent.check_options(options, allowed)
    if "ANTHROPIC_API_KEY" in options.env:
        problems.append("API key must not be part of selfcheck options")

    # The deny path works and lists alternatives.
    d = session.before_call("Bash", {"command": "id"})
    if d.allow or "R0" not in d.rules:
        problems.append("R0 does not deny a built-in tool call")
    d = session.before_call("mcp__blossoml__http_probe", {"target": "other"})
    if d.allow or "R6" not in d.rules:
        problems.append("R6 does not deny a non-allowlisted probe")

    if cfg.work_dir.exists() and any(cfg.work_dir.iterdir()):
        problems.append(f"work_dir {cfg.work_dir} must be empty")
    if not skip_secrets:
        from blossoml_ops.notify.keys import parse_secret_key

        for p in (cfg.anthropic_api_key_file, cfg.nostr_nsec_file):
            try:
                st = os.stat(p)
                if st.st_mode & 0o077:
                    problems.append(f"secret {p} must not be readable by group/others (chmod 0400)")
            except OSError:
                problems.append(f"secret {p} is missing")
        try:
            parse_secret_key(read_secret(cfg.nostr_nsec_file))
        except (ConfigError, ValueError):
            problems.append("nostr bot key cannot be parsed")
    return problems


def cmd_selfcheck(args) -> int:
    problems = selfcheck(args.config, args.skip_secrets)
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1
    print("selfcheck OK")
    return 0


def cmd_keygen(args) -> int:
    from blossoml_ops.notify.keys import bech32_encode, generate_secret, xonly_pubkey

    secret = generate_secret()
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "w") as f:
        f.write(bech32_encode("nsec", secret) + "\n")
    pub = xonly_pubkey(secret)
    _print({"npub": bech32_encode("npub", bytes.fromhex(pub)), "pubkey_hex": pub, "nsec_file": args.out})
    return 0


def cmd_publish_profile(args) -> int:
    from blossoml_ops.notify import RELAYS
    from blossoml_ops.notify.keys import finalize_event, parse_secret_key
    from blossoml_ops.notify.relay import publish

    cfg = load_config(args.config)
    secret = parse_secret_key(read_secret(cfg.nostr_nsec_file))
    content = {"name": args.name, "about": args.about, "bot": True}
    ev = finalize_event({"created_at": int(time.time()), "kind": 0, "tags": [], "content": json.dumps(content, ensure_ascii=False)}, secret)
    results = asyncio.run(publish(RELAYS, ev, auth_secret=secret, proxy=cfg.proxy, total_timeout=60))
    _print({r.relay: {"accepted": r.accepted, "message": r.message} for r in results})
    return 0 if any(r.accepted for r in results) else 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="blossoml-ops")
    p.add_argument("--config", default=os.environ.get("BLOSSOML_OPS_CONFIG", DEFAULT_CONFIG_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)
    for mode in ("daily", "postdeploy", "manual"):
        s = sub.add_parser(mode)
        s.set_defaults(func=cmd_run, mode=mode)
        if mode == "manual":
            s.add_argument("--instruction", help="investigation request (read from stdin if omitted)")
    s = sub.add_parser("selfcheck")
    s.add_argument("--skip-secrets", action="store_true")
    s.set_defaults(func=cmd_selfcheck)
    s = sub.add_parser("keygen")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_keygen)
    s = sub.add_parser("publish-profile")
    s.add_argument("--name", default="blossoML ops bot")
    s.add_argument("--about", default="blossoML の運用点検ボットです。運営者への報告のみを送ります。")
    s.set_defaults(func=cmd_publish_profile)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
