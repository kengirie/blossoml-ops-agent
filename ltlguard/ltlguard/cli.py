"""Command line: ``ltlguard compile | verify | check``.

- ``compile RULES.yaml -o RULES.dfa.json``: needs Spot.
- ``verify RULES.yaml RULES.dfa.json``: recompiles with Spot and checks that the
  committed file is semantically identical. Use in CI.
- ``check RULES.yaml RULES.dfa.json``: no Spot. Checks the hash and that the
  file loads. Use on hosts without Spot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ltlguard.core.dfa import CompiledRulesError, load_compiled, parse_compiled


def _compile(rules_path: Path) -> dict:
    from ltlguard.compile.spot_compiler import compile_spec
    from ltlguard.spec.loader import parse_spec

    import yaml

    source = rules_path.read_bytes()
    spec = parse_spec(yaml.safe_load(source.decode("utf-8")))
    return compile_spec(spec, source)


def dump_compiled(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def cmd_compile(args: argparse.Namespace) -> int:
    data = _compile(Path(args.rules))
    parse_compiled(data)  # self-check: the runtime must accept what we wrote
    out = Path(args.output) if args.output else Path(args.rules).with_suffix(".dfa.json")
    out.write_text(dump_compiled(data), encoding="utf-8")
    print(f"wrote {out} ({len(data['rules'])} rules, Spot {data['spot_version']})")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from ltlguard.compile.equivalence import ruleset_differences

    fresh = parse_compiled(_compile(Path(args.rules)))
    committed = load_compiled(args.compiled, args.rules)
    diffs = ruleset_differences(committed, fresh)
    if diffs:
        print(f"{args.compiled} does not match {args.rules}:", file=sys.stderr)
        for d in diffs:
            print(f"  - {d}", file=sys.stderr)
        print("run `ltlguard compile` and commit the result", file=sys.stderr)
        return 1
    print(f"{args.compiled} matches {args.rules}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    rs = load_compiled(args.compiled, args.rules)
    print(f"{args.compiled}: {len(rs.rules)} rules, hash OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ltlguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile", help="compile rules.yaml to rules.dfa.json (needs Spot)")
    c.add_argument("rules")
    c.add_argument("-o", "--output")
    c.set_defaults(func=cmd_compile)
    v = sub.add_parser("verify", help="recompile and compare with the committed file (needs Spot)")
    v.add_argument("rules")
    v.add_argument("compiled")
    v.set_defaults(func=cmd_verify)
    k = sub.add_parser("check", help="check the hash and load the compiled file (no Spot)")
    k.add_argument("rules")
    k.add_argument("compiled")
    k.set_defaults(func=cmd_check)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except CompiledRulesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
