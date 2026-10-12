"""Compiler tests and differential tests against Spot. Skipped without Spot."""

import itertools
import random
from pathlib import Path

import pytest

from conftest import requires_spot, rule_from_automaton

pytestmark = requires_spot

DATA = Path(__file__).parent / "data"

FORMULAS = [
    "G a",
    "F a",
    "G!(a)",
    "!(a) W (b)",
    "(!r W s) & (!r W l)",
    "G((a) -> F(b))",
    "G((a) -> X(G!(a)))",
    "G((a) -> X(G((a) -> X(G!(a)))))",
    "a U b",
    "X[!] a",
    "X a",
    "G(a -> X[!] b)",
    "F(a & X[!] F b)",
    "(a R b) | G F a",
    "a M b",
    "true",
    "false",
]


def spot_accepts(formula: str, word: list[frozenset[str]], aps: list[str], ended: bool) -> bool:
    """Spot's judgment: is there a trace with this prefix (or exactly this trace, if
    ``ended``) that satisfies the formula?"""
    import spot

    def letter(props):
        lits = [a if a in props else f"!{a}" for a in aps] or ["true"]
        return "(" + " & ".join(lits) + ")"

    tail = " & !X[!] true" if ended else ""
    w = letter(word[-1]) + tail
    for props in reversed(word[:-1]):
        w = f"{letter(props)} & X[!]({w})"
    f = spot.formula(f"({w}) & ({formula})")
    return not spot.ltlf_to_mtdfa(f).is_empty()


def compiled(formula):
    from ltlguard.compile.spot_compiler import compile_formula

    return rule_from_automaton("T", formula, compile_formula(formula))


@pytest.mark.parametrize("formula", FORMULAS)
def test_differential_random_traces(formula):
    rule = compiled(formula)
    aps = sorted(rule.aps) or ["a"]
    rng = random.Random(hash(formula) & 0xFFFF)
    letters = [frozenset(a for a, bit in zip(aps, bits) if bit) for bits in itertools.product((0, 1), repeat=len(aps))]
    words = set()
    for length in range(1, 4):  # exhaustive for short words
        for w in itertools.product(letters, repeat=length):
            words.add(w)
    for _ in range(40):  # random longer words
        words.add(tuple(rng.choice(letters) for _ in range(rng.randint(4, 7))))
    for w in sorted(words, key=lambda w: (len(w), [sorted(x) for x in w])):
        state = rule.initial
        for props in w:
            state = rule.step(state, props)
        s = rule.states[state]
        expected_accept = spot_accepts(formula, list(w), aps, ended=True)
        assert s.accepting == expected_accept, (formula, w)
        expected_alive = spot_accepts(formula, list(w), aps, ended=False) or expected_accept
        assert (not s.dead) == expected_alive, (formula, w)


@pytest.mark.parametrize("formula", FORMULAS)
def test_permanent_and_safe_next_consistent(formula):
    rule = compiled(formula)
    aps = sorted(rule.aps)
    letters = [frozenset(a for a, bit in zip(aps, bits) if bit) for bits in itertools.product((0, 1), repeat=len(aps))]
    for s in rule.states:
        for props in letters:
            nxt = rule.states[rule.step(s.id, props)]
            assert s.safe_next(props) == (not nxt.dead)
            if s.permanent:
                assert nxt.permanent and nxt.accepting
            if s.dead:
                assert nxt.dead


def test_empty_trace_semantics():
    from ltlguard.compile.spot_compiler import empty_trace_holds, parse_formula

    assert empty_trace_holds(parse_formula("G a"))
    assert not empty_trace_holds(parse_formula("F a"))
    assert empty_trace_holds(parse_formula("!a W b"))
    assert not empty_trace_holds(parse_formula("a U b"))
    assert empty_trace_holds(parse_formula("X a"))
    assert not empty_trace_holds(parse_formula("X[!] a"))


def test_fixture_is_up_to_date():
    from ltlguard.cli import main

    assert main(["verify", str(DATA / "fixture_rules.yaml"), str(DATA / "fixture_rules.dfa.json")]) == 0


def test_verify_detects_semantic_change(tmp_path):
    import json

    from ltlguard.cli import main

    y = DATA / "fixture_rules.yaml"
    data = json.loads((DATA / "fixture_rules.dfa.json").read_text())
    # Flip acceptance of a state of rule C (F notified).
    rule = next(r for r in data["rules"] if r["id"] == "C")
    rule["states"][0]["accepting"] = True
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(data))
    assert main(["verify", str(y), str(p)]) == 1
