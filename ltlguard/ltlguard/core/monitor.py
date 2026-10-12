"""Runtime monitor over a set of compiled rules.

- ``preview``: try one step with the call-time propositions without changing
  state (PreToolUse). Result propositions are unknown at that point, so a rule
  is reported as violated only if *every* subset of the possible result
  propositions leads to a dead state.
- ``commit``: take one step with call-time and result propositions (PostToolUse).

State is advanced only on commit, so calls that are denied, or stopped by
another mechanism, never move the automata.
"""

from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field
from typing import AbstractSet, Iterable, Mapping, Sequence

from ltlguard.core.dfa import CompiledRule, RuleSet
from ltlguard.core.labeling import Candidate

# Bound on the number of result propositions considered per call at preview.
_MAX_RESULT_PROPS = 8


class Verdict(str, enum.Enum):
    SATISFIED = "satisfied"                       # 確定成立
    VIOLATED = "violated"                         # 確定違反
    TENTATIVE_OK = "tentative_ok"                 # 暫定成立（今終われば成立）
    TENTATIVE_VIOLATION = "tentative_violation"   # 暫定違反（未履行の義務あり）


def verdict_of(rule: CompiledRule, state: int) -> Verdict:
    s = rule.states[state]
    if s.dead:
        return Verdict.VIOLATED
    if s.permanent:
        return Verdict.SATISFIED
    if s.accepting:
        return Verdict.TENTATIVE_OK
    return Verdict.TENTATIVE_VIOLATION


@dataclass
class PreviewResult:
    denied_by: list[CompiledRule] = field(default_factory=list)      # enforced rules that would be violated
    shadow_denied_by: list[CompiledRule] = field(default_factory=list)  # log_only rules that would be violated

    @property
    def allowed(self) -> bool:
        return not self.denied_by


def _subsets(props: AbstractSet[str]) -> Iterable[frozenset[str]]:
    items = sorted(props)
    if len(items) > _MAX_RESULT_PROPS:
        raise ValueError(f"too many possible result propositions: {len(items)}")
    for r in range(len(items) + 1):
        for combo in itertools.combinations(items, r):
            yield frozenset(combo)


class Monitor:
    def __init__(self, ruleset: RuleSet, states: Mapping[str, int] | None = None):
        self.ruleset = ruleset
        self.states: dict[str, int] = {r.id: r.initial for r in ruleset.rules}
        if states:
            for rid, st in states.items():
                rule = ruleset.by_id(rid)
                if not 0 <= st < len(rule.states):
                    raise ValueError(f"state {st} out of range for rule {rid}")
                self.states[rid] = st
        self.steps = 0

    @property
    def rules(self) -> Sequence[CompiledRule]:
        return self.ruleset.rules

    def _would_violate(self, rule: CompiledRule, call_props: frozenset[str], possible: frozenset[str]) -> bool:
        current = self.states[rule.id]
        if rule.states[current].dead:
            # Already violated (and recorded). It no longer gates further calls.
            return False
        for extra in _subsets(possible & rule.aps):
            if not rule.states[rule.step(current, call_props | extra)].dead:
                return False
        return True

    def preview(self, call_props: AbstractSet[str], possible_result_props: AbstractSet[str] = frozenset()) -> PreviewResult:
        call = frozenset(call_props)
        possible = frozenset(possible_result_props) - call
        out = PreviewResult()
        for rule in self.rules:
            if self._would_violate(rule, call, possible):
                (out.denied_by if rule.enforced else out.shadow_denied_by).append(rule)
        return out

    def commit(self, props: AbstractSet[str]) -> list[CompiledRule]:
        """Advance every rule by one step. Returns rules that became violated by this step."""
        props = frozenset(props)
        new_states = {r.id: r.step(self.states[r.id], props) for r in self.rules}
        newly = [
            r for r in self.rules
            if r.states[new_states[r.id]].dead and not r.states[self.states[r.id]].dead
        ]
        self.states = new_states
        self.steps += 1
        return newly

    def verdicts(self) -> dict[str, Verdict]:
        return {r.id: verdict_of(r, self.states[r.id]) for r in self.rules}

    def pending_obligations(self) -> list[CompiledRule]:
        """Enforced rules that would be violated if the run ended now (but still can be met)."""
        return [r for r in self.rules if r.enforced and verdict_of(r, self.states[r.id]) is Verdict.TENTATIVE_VIOLATION]

    def violated(self) -> list[CompiledRule]:
        return [r for r in self.rules if r.states[self.states[r.id]].dead]

    def allowed_candidates(self, candidates: Iterable[tuple[Candidate, frozenset[str], frozenset[str]]]) -> list[str]:
        """Labels of candidates (with their call props and possible result props)
        that no enforced rule would deny. This is the intersection of the rules'
        ``safe_next`` sets, evaluated on each candidate."""
        out = []
        for cand, call, possible in candidates:
            if self.preview(call, possible).allowed:
                out.append(cand.label)
        return out

    def snapshot(self) -> dict:
        return {
            "source_sha256": self.ruleset.source_sha256,
            "steps": self.steps,
            "states": dict(self.states),
            "verdicts": {k: v.value for k, v in self.verdicts().items()},
        }

    def describe(self) -> dict[str, dict]:
        return {
            rid: {"state": st, "verdict": verdict_of(self.ruleset.by_id(rid), st).value}
            for rid, st in self.states.items()
        }
