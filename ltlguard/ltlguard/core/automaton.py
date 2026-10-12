"""Rules as Spot automata.

Each rule's LTLf formula is translated by Spot (``ltlf_to_mtdfa``, then
``minimize_mtdfa``) and used through ``mtdfa.as_twa``: a deterministic
automaton whose states are labeled with the *residual* LTLf formula (what the
rest of the trace must satisfy) and whose transitions carry a final mark: a
non-empty trace is accepted iff its last transition is marked. Missing
transitions lead to rejection forever.

Monitoring needs, per position: is the trace accepted if it ends here, can it
still be accepted (otherwise the rule is definitively violated), and must it
be accepted whatever comes next. All three come from Spot:

- ending here: the final mark of the last transition taken;
- still possible: the residual formula is satisfiable
  (``not ltlf_to_mtdfa(residual).is_empty()``);
- always accepted: the residual formula is valid
  (``ltlf_to_mtdfa(!residual).is_empty()``).

Steps are taken by intersecting each transition's BDD guard with the BDD cube
of the propositions true at that step.

Spot defines LTLf on non-empty traces. For the state before any event (the
empty trace) the natural extension is used: weak operators (``G``, ``W``,
``R``, weak ``X``) hold and strong ones (``F``, ``U``, ``M``, ``X[!]``) and
atoms do not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Iterable

import buddy
import spot

from ltlguard.spec.loader import RuleSpec, Spec, SpecError, load_spec

MIN_SPOT = (2, 14)
SINK = -1  # no transition was enabled: rejected forever


class RulesError(Exception):
    """The rules cannot be loaded. Callers must refuse to run."""


def check_spot_version() -> None:
    parts = spot.version().split(".")
    try:
        version = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        raise RulesError(f"cannot parse Spot version {spot.version()!r}") from None
    if version < MIN_SPOT:
        raise RulesError(f"Spot >= {MIN_SPOT[0]}.{MIN_SPOT[1]} is required (found {spot.version()})")


def empty_trace_holds(f: "spot.formula") -> bool:
    """Truth of ``f`` on the empty trace (see the module docstring)."""
    k = f.kind()
    if k == spot.op_tt:
        return True
    if k in (spot.op_ff, spot.op_ap):
        return False
    if k == spot.op_Not:
        return not empty_trace_holds(f[0])
    if k == spot.op_And:
        return all(empty_trace_holds(c) for c in f)
    if k == spot.op_Or:
        return any(empty_trace_holds(c) for c in f)
    if k == spot.op_Implies:
        return (not empty_trace_holds(f[0])) or empty_trace_holds(f[1])
    if k == spot.op_Equiv:
        return empty_trace_holds(f[0]) == empty_trace_holds(f[1])
    if k == spot.op_Xor:
        return empty_trace_holds(f[0]) != empty_trace_holds(f[1])
    if k in (spot.op_X, spot.op_G, spot.op_W, spot.op_R):
        return True
    if k in (spot.op_strong_X, spot.op_F, spot.op_U, spot.op_M):
        return False
    raise RulesError(f"unsupported operator in LTLf formula: {f.kindstr()}")


@dataclass(frozen=True)
class RuleState:
    """Where a rule is: a Spot state (or SINK), and whether the trace so far is accepted."""

    node: int
    final: bool
    initial: bool = False

    def to_json(self) -> dict:
        return {"node": self.node, "final": self.final, "initial": self.initial}

    @classmethod
    def from_json(cls, d: dict) -> "RuleState":
        return cls(int(d["node"]), bool(d["final"]), bool(d.get("initial", False)))


class Rule:
    def __init__(self, spec: RuleSpec):
        self.id = spec.id
        self.name = spec.name
        self.description = spec.description
        self.on_violation = spec.on_violation
        self.alert = spec.alert
        self.formula = spec.formula
        self.formula_text = str(spec.formula)
        try:
            self.automaton = spot.minimize_mtdfa(spot.ltlf_to_mtdfa(spec.formula)).as_twa(False, True)
        except Exception as e:
            raise RulesError(f"rule {spec.id}: Spot cannot translate the formula: {e}") from None
        self._dict = self.automaton.get_dict()
        self._vars = [(str(ap), self._dict.varnum(ap)) for ap in self.automaton.ap()]
        self.aps = frozenset(str(ap) for ap in spot.atomic_prop_collect(spec.formula))
        names = list(self.automaton.get_state_names())
        if len(names) != self.automaton.num_states():
            raise RulesError(f"rule {spec.id}: Spot did not label the states with residual formulas")
        self.residuals = [spot.formula(n) for n in names]
        # Satisfiable / valid residuals, decided by Spot.
        self._satisfiable = [not spot.ltlf_to_mtdfa(r).is_empty() for r in self.residuals]
        self._valid = [spot.ltlf_to_mtdfa(spot.formula.Not(r)).is_empty() for r in self.residuals]
        self.initial = RuleState(self.automaton.get_init_state_number(), empty_trace_holds(spec.formula), True)

    @property
    def enforced(self) -> bool:
        return self.on_violation == "deny"

    def _cube(self, true_props: AbstractSet[str]):
        cube = buddy.bddtrue
        for name, var in self._vars:
            cube &= buddy.bdd_ithvar(var) if name in true_props else buddy.bdd_nithvar(var)
        return cube

    def step(self, state: RuleState, true_props: AbstractSet[str]) -> RuleState:
        if state.node == SINK:
            return state
        cube = self._cube(true_props)
        for e in self.automaton.out(state.node):
            if (e.cond & cube) != buddy.bddfalse:
                return RuleState(e.dst, bool(e.acc.has(0)))
        return RuleState(SINK, False)

    def accepting(self, state: RuleState) -> bool:
        return state.final

    def dead(self, state: RuleState) -> bool:
        """Definitively violated: not accepted now, and no continuation is."""
        if state.node == SINK:
            return True
        return not state.final and not self._satisfiable[state.node]

    def permanent(self, state: RuleState) -> bool:
        """Definitively satisfied: accepted now, and after any continuation."""
        return state.node != SINK and state.final and self._valid[state.node]

    def residual(self, state: RuleState) -> str:
        return "0" if state.node == SINK else str(self.residuals[state.node])

    def to_hoa(self) -> str:
        return self.automaton.to_str("hoa")


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    source_sha256: str

    def by_id(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    @property
    def aps(self) -> frozenset[str]:
        out: frozenset[str] = frozenset()
        for r in self.rules:
            out |= r.aps
        return out


def build_ruleset(spec: Spec) -> RuleSet:
    check_spot_version()
    return RuleSet(rules=tuple(Rule(r) for r in spec.rules), source_sha256=spec.source_sha256)


def load_rules(path: str | Path) -> RuleSet:
    """Read ``rules.yaml`` and translate every rule with Spot."""
    try:
        return build_ruleset(load_spec(path))
    except SpecError as e:
        raise RulesError(str(e)) from None


def run_word(rule: Rule, word: Iterable[AbstractSet[str]]) -> RuleState:
    state = rule.initial
    for props in word:
        state = rule.step(state, props)
    return state
