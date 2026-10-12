"""Compiled rules (``rules.dfa.json``) and their loading.

The file is produced by ``ltlguard compile`` (which uses Spot). Loading it does
not need Spot. Each rule is a complete deterministic finite automaton with
state-based acceptance and these precomputed per-state values:

- ``accepting``: a trace ending here satisfies the rule
- ``dead``: no continuation can reach an accepting state (definitive violation)
- ``permanent``: every continuation stays accepting (definitive satisfaction)
- ``safe_next``: guard of the transitions that do not lead to a dead state
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Iterable, Mapping

from ltlguard.core.guard_expr import Guard, props_of

FORMAT = "ltlguard-dfa/1"
ON_VIOLATION_VALUES = ("deny", "log_only")
# Exhaustive determinism/completeness checks are skipped above this many APs.
_MAX_APS_FOR_EXHAUSTIVE_CHECK = 12


class CompiledRulesError(Exception):
    """The compiled rules are missing, stale, or malformed. Callers must refuse to run."""


@dataclass(frozen=True)
class State:
    id: int
    accepting: bool
    dead: bool
    permanent: bool
    safe_next: Guard
    transitions: tuple[tuple[Guard, int], ...]


@dataclass(frozen=True)
class CompiledRule:
    id: str
    name: str
    description: str
    formula: str
    on_violation: str
    alert: str | None
    aps: frozenset[str]
    initial: int
    states: tuple[State, ...]

    @property
    def enforced(self) -> bool:
        return self.on_violation == "deny"

    def step(self, state: int, true_props: AbstractSet[str]) -> int:
        props = true_props & self.aps
        for guard, dst in self.states[state].transitions:
            if guard(props):
                return dst
        # A complete DFA always has a matching transition. Fail closed.
        raise CompiledRulesError(f"rule {self.id}: no transition from state {state} on {sorted(props)}")


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[CompiledRule, ...]
    source_sha256: str
    metadata: Mapping[str, object]

    def by_id(self, rule_id: str) -> CompiledRule:
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


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_rule(raw: Mapping) -> CompiledRule:
    try:
        aps = frozenset(raw["aps"])
        states_raw = raw["states"]
        states = []
        for i, s in enumerate(states_raw):
            if s["id"] != i:
                raise CompiledRulesError(f"rule {raw['id']}: state ids must be 0..n-1 in order")
            transitions = tuple((Guard(t["guard"]), int(t["to"])) for t in s["transitions"])
            states.append(
                State(
                    id=i,
                    accepting=bool(s["accepting"]),
                    dead=bool(s["dead"]),
                    permanent=bool(s["permanent"]),
                    safe_next=Guard(s["safe_next"]),
                    transitions=transitions,
                )
            )
        on_violation = raw["on_violation"]
        if on_violation not in ON_VIOLATION_VALUES:
            raise CompiledRulesError(f"rule {raw['id']}: unsupported on_violation {on_violation!r}")
        rule = CompiledRule(
            id=str(raw["id"]),
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            formula=str(raw["formula"]),
            on_violation=on_violation,
            alert=raw.get("alert"),
            aps=aps,
            initial=int(raw["initial"]),
            states=tuple(states),
        )
    except CompiledRulesError:
        raise
    except Exception as e:  # malformed input of any kind
        raise CompiledRulesError(f"malformed rule entry: {e!r}") from e
    _check_rule(rule)
    return rule


def _check_rule(rule: CompiledRule) -> None:
    n = len(rule.states)
    if not 0 <= rule.initial < n:
        raise CompiledRulesError(f"rule {rule.id}: initial state out of range")
    for s in rule.states:
        for guard, dst in s.transitions:
            if not 0 <= dst < n:
                raise CompiledRulesError(f"rule {rule.id}: transition target out of range")
            unknown = props_of(guard.node) - rule.aps
            if unknown:
                raise CompiledRulesError(f"rule {rule.id}: guard uses undeclared APs {sorted(unknown)}")
    if len(rule.aps) > _MAX_APS_FOR_EXHAUSTIVE_CHECK:
        return
    aps = sorted(rule.aps)
    valuations = [
        frozenset(a for a, bit in zip(aps, bits) if bit)
        for bits in itertools.product((False, True), repeat=len(aps))
    ]
    for s in rule.states:
        for val in valuations:
            matches = [dst for guard, dst in s.transitions if guard(val)]
            if len(matches) != 1:
                raise CompiledRulesError(
                    f"rule {rule.id}: state {s.id} is not deterministic/complete on {sorted(val)}"
                )
            safe = not rule.states[matches[0]].dead
            if safe != s.safe_next(val):
                raise CompiledRulesError(f"rule {rule.id}: safe_next of state {s.id} is inconsistent")


def parse_compiled(data: Mapping) -> RuleSet:
    if data.get("format") != FORMAT:
        raise CompiledRulesError(f"unsupported format {data.get('format')!r}")
    rules = tuple(_parse_rule(r) for r in data.get("rules", []))
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise CompiledRulesError("duplicate rule ids")
    meta = {k: v for k, v in data.items() if k != "rules"}
    return RuleSet(rules=rules, source_sha256=str(data.get("source_sha256", "")), metadata=meta)


def load_compiled(compiled_path: str | Path, source_path: str | Path | None = None) -> RuleSet:
    """Load ``rules.dfa.json``.

    If ``source_path`` (the ``rules.yaml``) is given, its SHA-256 must match the
    one recorded at compile time; otherwise the rules were edited without being
    recompiled and we refuse to run.
    """
    try:
        data = json.loads(Path(compiled_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CompiledRulesError(f"cannot read {compiled_path}: {e}") from e
    ruleset = parse_compiled(data)
    if source_path is not None:
        actual = sha256_file(source_path)
        if actual != ruleset.source_sha256:
            raise CompiledRulesError(
                f"{compiled_path} was compiled from a different {Path(source_path).name} "
                f"(expected sha256 {ruleset.source_sha256}, got {actual}); run `ltlguard compile`"
            )
    return ruleset


def run_word(rule: CompiledRule, word: Iterable[AbstractSet[str]]) -> int:
    """Run ``rule`` over a sequence of proposition sets and return the final state."""
    state = rule.initial
    for props in word:
        state = rule.step(state, props)
    return state
