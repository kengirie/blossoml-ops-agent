"""The monitor's verdicts against Spot's own judgment of concrete traces.

The automata come from Spot, so this checks the plumbing around them: the
initial state, missing transitions, final marks, and the dead/permanent
classification from residual formulas.
"""

import itertools
import random

import pytest
import spot

from ltlguard.core.automaton import Rule, empty_trace_holds
from ltlguard.spec.loader import RuleSpec, parse_formula

FORMULAS = [
    "G a",
    "F a",
    "G !a",
    "!a W b",
    "(!r W s) & (!r W l)",
    "G(a -> F b)",
    "G(a -> X G !a)",
    "G(a -> X G(a -> X G !a))",
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


def rule(formula: str) -> Rule:
    return Rule(RuleSpec("T", "T", "", parse_formula(formula), "deny", None))


def spot_says(formula: str, word, aps, ended: bool) -> bool:
    """Is there a trace with this prefix (or exactly this trace if ``ended``) satisfying the formula?"""
    def letter(props):
        lits = [a if a in props else f"!{a}" for a in aps] or ["true"]
        return "(" + " & ".join(lits) + ")"

    w = letter(word[-1]) + (" & !X[!] true" if ended else "")
    for props in reversed(word[:-1]):
        w = f"{letter(props)} & X[!]({w})"
    return not spot.ltlf_to_mtdfa(spot.formula(f"({w}) & ({formula})")).is_empty()


def words(aps, seed):
    rng = random.Random(seed)
    letters = [frozenset(a for a, bit in zip(aps, bits) if bit) for bits in itertools.product((0, 1), repeat=len(aps))]
    out = set()
    for n in range(1, 4):
        out.update(itertools.product(letters, repeat=n))
    for _ in range(40):
        out.add(tuple(rng.choice(letters) for _ in range(rng.randint(4, 7))))
    return sorted(out, key=lambda w: (len(w), [sorted(x) for x in w]))


@pytest.mark.parametrize("formula", FORMULAS)
def test_verdicts_match_spot(formula):
    r = rule(formula)
    aps = sorted(r.aps) or ["a"]
    for w in words(aps, sum(map(ord, formula))):
        state = r.initial
        for props in w:
            state = r.step(state, props)
        accepted = spot_says(formula, list(w), aps, ended=True)
        alive = accepted or spot_says(formula, list(w), aps, ended=False)
        assert r.accepting(state) == accepted, (formula, w)
        assert (not r.dead(state)) == alive, (formula, w)
        if r.permanent(state):
            # Every one-letter extension must still be accepted.
            for x in words(aps, 0)[: 2 ** len(aps)]:
                assert spot_says(formula, list(w) + list(x), aps, ended=True), (formula, w, x)


@pytest.mark.parametrize(
    "formula,expected",
    [("G a", True), ("F a", False), ("!a W b", True), ("a U b", False), ("X a", True), ("X[!] a", False), ("true", True)],
)
def test_empty_trace(formula, expected):
    assert empty_trace_holds(parse_formula(formula)) is expected
    assert rule(formula).accepting(rule(formula).initial) is expected
