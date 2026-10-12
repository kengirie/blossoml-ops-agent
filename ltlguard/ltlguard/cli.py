"""Command line: ``ltlguard check | show``.

- ``check RULES.yaml``: parse and translate every rule with Spot; print a summary.
- ``show RULES.yaml [--rule ID] [--format hoa|dot]``: print Spot's automata for review.
"""

from __future__ import annotations

import argparse
import sys

from ltlguard.core.automaton import RulesError, load_rules


def cmd_check(args: argparse.Namespace) -> int:
    rs = load_rules(args.rules)
    import spot

    print(f"{args.rules}: {len(rs.rules)} rules (Spot {spot.version()}, sha256 {rs.source_sha256[:12]})")
    for r in rs.rules:
        print(f"  {r.id:<4} {r.on_violation:<8} {r.automaton.num_states():>2} states  {r.formula_text}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    rs = load_rules(args.rules)
    for r in rs.rules:
        if args.rule and r.id != args.rule:
            continue
        print(f"// {r.id}: {r.formula_text}")
        print(r.automaton.to_str(args.format))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ltlguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="parse and translate the rules with Spot")
    c.add_argument("rules")
    c.set_defaults(func=cmd_check)
    s = sub.add_parser("show", help="print the automata (HOA or dot)")
    s.add_argument("rules")
    s.add_argument("--rule")
    s.add_argument("--format", choices=("hoa", "dot"), default="hoa")
    s.set_defaults(func=cmd_show)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except RulesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
