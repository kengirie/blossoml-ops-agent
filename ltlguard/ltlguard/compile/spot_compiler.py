"""LTLf -> DFA with Spot (>= 2.14), and the precomputation for the runtime.

Path: ``spot.ltlf_to_mtdfa`` -> ``spot.minimize_mtdfa`` -> ``mtdfa.as_twa``.

``as_twa`` returns a deterministic automaton with *transition-based* final
marks: a non-empty word is accepted iff its last transition is marked. Missing
transitions go to an implicit rejecting sink. ltlguard uses *state-based*
acceptance, so each state of the result is a pair (Spot state, mark of the
transition that entered it), plus a fresh initial state and an explicit sink.

LTLf in Spot is defined on non-empty words. For the initial state (the empty
trace, before any tool call) we use the natural extension where weak
operators (``G``, ``W``, ``R``, weak ``X``) hold and strong ones (``F``, ``U``,
``M``, ``X[!]``) and atoms do not.
"""

from __future__ import annotations

import collections
import hashlib
from typing import Any

import spot
import buddy

from ltlguard import __version__
from ltlguard.core.dfa import FORMAT
from ltlguard.spec.loader import RuleSpec, Spec, SpecError


class CompileError(Exception):
    pass


def spot_version() -> str:
    return spot.version()


def _check_spot_version() -> None:
    parts = spot.version().split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise CompileError(f"cannot parse Spot version {spot.version()!r}") from None
    if (major, minor) < (2, 14):
        raise CompileError(f"Spot >= 2.14 is required (found {spot.version()})")


def empty_trace_holds(f: "spot.formula") -> bool:
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
    raise CompileError(f"unsupported operator in LTLf formula: {f.kindstr()}")


def _formula_to_guard(f: "spot.formula") -> str:
    """Render a propositional Spot formula in ltlguard's guard syntax."""
    k = f.kind()
    if k == spot.op_tt:
        return "true"
    if k == spot.op_ff:
        return "false"
    if k == spot.op_ap:
        return f.ap_name()
    if k == spot.op_Not:
        return "!" + _formula_to_guard(f[0])
    if k in (spot.op_And, spot.op_Or):
        sep = " & " if k == spot.op_And else " | "
        return "(" + sep.join(_formula_to_guard(c) for c in f) + ")"
    raise CompileError(f"unexpected operator in guard: {f.kindstr()}")


def _bdd_to_guard(b: Any, bdict: Any) -> str:
    if b == buddy.bddtrue:
        return "true"
    if b == buddy.bddfalse:
        return "false"
    text = _formula_to_guard(spot.bdd_to_formula(b, bdict))
    if text.startswith("(") and text.endswith(")"):
        # Drop the outermost parentheses only if they enclose the whole text.
        depth = 0
        for i, ch in enumerate(text):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(text) - 1:
                break
        else:
            text = text[1:-1]
    return text


def parse_formula(text: str) -> "spot.formula":
    pf = spot.parse_infix_psl(text)
    if pf.errors:
        raise CompileError(f"cannot parse formula {text!r}")
    f = pf.f
    if not f.is_ltl_formula():
        raise CompileError(f"not an LTL formula: {text!r}")
    return f


def _minimize(accepting: list[bool], edges: list[dict[int, Any]]) -> tuple[list[bool], list[dict[int, Any]]]:
    """Moore minimization on a complete DFA whose edges are BDD-labeled.

    BDDs are canonical, so two states are equivalent when, for every block, the
    disjunction of their guards into that block is the same BDD. States are
    renumbered in BFS order from the initial state 0 so the output is stable.
    """
    n = len(accepting)
    block = [int(a) for a in accepting]
    while True:
        sigs: dict[tuple, int] = {}
        new_block = []
        for i in range(n):
            into: dict[int, Any] = {}
            for j, cond in edges[i].items():
                into[block[j]] = into.get(block[j], buddy.bddfalse) | cond
            sig = (block[i], tuple(sorted((b, c.id()) for b, c in into.items())))
            new_block.append(sigs.setdefault(sig, len(sigs)))
        if len(sigs) == len(set(block)):
            break
        block = new_block
    # Renumber blocks in BFS order from the initial state.
    order: dict[int, int] = {block[0]: 0}
    queue = collections.deque([0])
    rep: dict[int, int] = {block[0]: 0}
    while queue:
        i = queue.popleft()
        for j in sorted(edges[i], key=lambda j: j):
            if block[j] not in order:
                order[block[j]] = len(order)
                rep[block[j]] = j
                queue.append(j)
    m = len(order)
    new_acc = [False] * m
    new_edges: list[dict[int, Any]] = [dict() for _ in range(m)]
    for b, i in rep.items():
        k = order[b]
        new_acc[k] = accepting[i]
        for j, cond in edges[i].items():
            t = order[block[j]]
            new_edges[k][t] = new_edges[k].get(t, buddy.bddfalse) | cond
    return new_acc, new_edges


def compile_formula(text: str) -> dict:
    """Compile one LTLf formula into the JSON automaton (without rule metadata)."""
    f = parse_formula(text)
    mtdfa = spot.minimize_mtdfa(spot.ltlf_to_mtdfa(f))
    aut = mtdfa.as_twa(False, False)
    bdict = aut.get_dict()
    aps = sorted(str(a) for a in aut.ap())
    formula_aps = sorted(str(a) for a in spot.atomic_prop_collect(f))
    # APs that simplification removed still belong to the rule (they are irrelevant, not unknown).
    aps = sorted(set(aps) | set(formula_aps))

    # Our states: "init", (spot_state, mark) pairs, and "sink".
    INIT, SINK = ("init",), ("sink",)
    index: dict[tuple, int] = {INIT: 0}
    order = [INIT]
    queue = collections.deque([INIT])
    edges: dict[tuple, list[tuple[Any, tuple]]] = {}

    def add(key: tuple) -> None:
        if key not in index:
            index[key] = len(order)
            order.append(key)
            queue.append(key)

    init_spot = aut.get_init_state_number()
    while queue:
        key = queue.popleft()
        if key == SINK:
            edges[key] = [(buddy.bddtrue, SINK)]
            continue
        spot_state = init_spot if key == INIT else key[0]
        out: list[tuple[Any, tuple]] = []
        covered = buddy.bddfalse
        for e in aut.out(spot_state):
            dst = (e.dst, bool(e.acc.has(0)))
            out.append((e.cond, dst))
            covered = covered | e.cond
            add(dst)
        rest = buddy.bddtrue - covered
        if rest != buddy.bddfalse:
            out.append((rest, SINK))
            add(SINK)
        edges[key] = out

    n = len(order)
    accepting = [False] * n
    accepting[0] = empty_trace_holds(f)
    for key, i in index.items():
        if key not in (INIT, SINK):
            accepting[i] = key[1]
    raw_edges: list[dict[int, Any]] = [dict() for _ in range(n)]
    for key, out in edges.items():
        merged = raw_edges[index[key]]
        for cond, dst in out:
            j = index[dst]
            merged[j] = merged.get(j, buddy.bddfalse) | cond

    accepting, trans = _minimize(accepting, raw_edges)
    n = len(accepting)

    pred: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in trans[i]:
            pred[j].add(i)

    def backward_closure(seed: set[int]) -> set[int]:
        seen = set(seed)
        stack = list(seed)
        while stack:
            s = stack.pop()
            for p in pred[s]:
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen

    can_accept = backward_closure({i for i in range(n) if accepting[i]})
    can_reject = backward_closure({i for i in range(n) if not accepting[i]})
    dead = [i not in can_accept for i in range(n)]
    permanent = [i not in can_reject for i in range(n)]

    states = []
    for i in range(n):
        safe = buddy.bddfalse
        for j, cond in trans[i].items():
            if not dead[j]:
                safe = safe | cond
        states.append(
            {
                "id": i,
                "accepting": accepting[i],
                "dead": dead[i],
                "permanent": permanent[i],
                "safe_next": _bdd_to_guard(safe, bdict),
                "transitions": [
                    {"guard": _bdd_to_guard(cond, bdict), "to": j} for j, cond in sorted(trans[i].items())
                ],
            }
        )
    return {"aps": aps, "initial": 0, "states": states}


def compile_rule(rule: RuleSpec) -> dict:
    try:
        automaton = compile_formula(rule.formula)
    except CompileError as e:
        raise CompileError(f"rule {rule.id}: {e}") from None
    return {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "formula": rule.formula,
        "on_violation": rule.on_violation,
        "alert": rule.alert,
        **automaton,
    }


def compile_spec(spec: Spec, source_bytes: bytes) -> dict:
    _check_spot_version()
    return {
        "format": FORMAT,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "compiler": f"ltlguard {__version__}",
        "spot_version": spot.version(),
        "rules": [compile_rule(r) for r in spec.rules],
    }


__all__ = ["CompileError", "SpecError", "compile_formula", "compile_rule", "compile_spec", "empty_trace_holds", "parse_formula", "spot_version"]
