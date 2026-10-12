"""Semantic comparison of two compiled rule sets (no Spot needed).

Used by CI to check that the committed ``rules.dfa.json`` matches a fresh
compile even if guard strings or state numbering differ (e.g. another Spot
version). Two rules match when their automata are bisimilar with respect to
the labels (accepting, dead, permanent).
"""

from __future__ import annotations

import collections
import itertools

from ltlguard.core.dfa import CompiledRule, RuleSet

_META_FIELDS = ("name", "description", "formula", "on_violation", "alert")


def rule_differences(a: CompiledRule, b: CompiledRule) -> list[str]:
    diffs = [f"{f} differs" for f in _META_FIELDS if getattr(a, f) != getattr(b, f)]
    aps = sorted(a.aps | b.aps)
    if len(aps) > 16:
        return diffs + ["too many APs to compare exhaustively"]
    valuations = [
        frozenset(p for p, bit in zip(aps, bits) if bit)
        for bits in itertools.product((False, True), repeat=len(aps))
    ]
    seen = {(a.initial, b.initial)}
    queue = collections.deque(seen)
    while queue:
        sa, sb = queue.popleft()
        xa, xb = a.states[sa], b.states[sb]
        if (xa.accepting, xa.dead, xa.permanent) != (xb.accepting, xb.dead, xb.permanent):
            return diffs + [f"state labels differ at product state ({sa}, {sb})"]
        for val in valuations:
            nxt = (a.step(sa, val), b.step(sb, val))
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return diffs


def ruleset_differences(a: RuleSet, b: RuleSet) -> list[str]:
    diffs = []
    if a.source_sha256 != b.source_sha256:
        diffs.append("source_sha256 differs")
    ids_a = [r.id for r in a.rules]
    ids_b = [r.id for r in b.rules]
    if ids_a != ids_b:
        return diffs + [f"rule ids differ: {ids_a} vs {ids_b}"]
    for ra, rb in zip(a.rules, b.rules):
        diffs += [f"{ra.id}: {d}" for d in rule_differences(ra, rb)]
    return diffs
