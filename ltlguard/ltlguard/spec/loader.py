"""Rule files (YAML) and template expansion.

Example::

    version: 1
    rules:
      - id: R1
        name: 再起動の前に状態とログを確認する
        description: restart_service を呼ぶ前に、service_status と read_logs を実行してください。
        formula: "(!restart W status_checked) & (!restart W logs_read)"
        on_violation: deny
      - id: R4
        name: 再起動後は稼働確認する
        template: response
        trigger: restart
        response: probe_ok

Formulas use Spot's LTLf syntax: ``!``, ``&``, ``|``, ``->``, ``<->``, ``X``
(weak next), ``X[!]`` (strong next), ``F``, ``G``, ``U``, ``W``, ``R``, ``M``.
Proposition names are lowercase identifiers; their meaning is defined by the
labeling function, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from ltlguard.core.dfa import ON_VIOLATION_VALUES
from ltlguard.core.guard_expr import PROP_NAME_RE, RESERVED_NAMES

RULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_'.-]{0,31}$")
ALERT_VALUES = (None, "info", "warning", "critical")

# Arguments are boolean expressions over propositions, e.g. "restart | rollback".
TEMPLATES: dict[str, tuple[str, ...]] = {
    "always": ("prop",),
    "never": ("prop",),
    "eventually": ("prop",),
    "not_until": ("prop", "until"),
    "response": ("trigger", "response"),
    "at_most_once": ("prop",),
    "at_most": ("prop", "count"),
}
_MAX_AT_MOST = 20

# Words that may appear in formulas without being propositions.
_LTL_KEYWORDS = frozenset({"X", "F", "G", "U", "W", "R", "M", "true", "false"})
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ARG_CHARS_RE = re.compile(r"^[a-z0-9_()!&|\s]+$")


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class RuleSpec:
    id: str
    name: str
    description: str
    formula: str
    on_violation: str
    alert: str | None
    source_template: str | None = None


@dataclass(frozen=True)
class Spec:
    version: int
    rules: tuple[RuleSpec, ...]


def _check_bool_arg(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"template argument {name!r} must be a non-empty string")
    if not _ARG_CHARS_RE.match(value):
        raise SpecError(f"template argument {name!r} may only use propositions, !, &, |, parentheses: {value!r}")
    return f"({value.strip()})"


def expand_template(template: str, args: Mapping[str, Any]) -> str:
    if template not in TEMPLATES:
        raise SpecError(f"unknown template {template!r} (known: {', '.join(sorted(TEMPLATES))})")
    if template == "always":
        return f"G{_check_bool_arg('prop', args.get('prop'))}"
    if template == "never":
        return f"G!{_check_bool_arg('prop', args.get('prop'))}"
    if template == "eventually":
        return f"F{_check_bool_arg('prop', args.get('prop'))}"
    if template == "not_until":
        a = _check_bool_arg("prop", args.get("prop"))
        b = _check_bool_arg("until", args.get("until"))
        return f"!{a} W {b}"
    if template == "response":
        a = _check_bool_arg("trigger", args.get("trigger"))
        b = _check_bool_arg("response", args.get("response"))
        return f"G({a} -> F{b})"
    if template == "at_most_once":
        return expand_template("at_most", {"prop": args.get("prop"), "count": 1})
    if template == "at_most":
        a = _check_bool_arg("prop", args.get("prop"))
        n = args.get("count")
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= _MAX_AT_MOST:
            raise SpecError(f"at_most: count must be an integer in 0..{_MAX_AT_MOST}")
        # f_0 = G !a ; f_k = G(a -> X f_{k-1})   (X is the weak next of LTLf)
        f = f"G!{a}"
        for _ in range(n):
            f = f"G({a} -> X({f}))"
        return f
    raise AssertionError(template)


def formula_props(formula: str) -> frozenset[str]:
    """Identifiers in a formula that are propositions (best effort; Spot is authoritative)."""
    return frozenset(w for w in _IDENT_RE.findall(formula) if w not in _LTL_KEYWORDS)


def _parse_rule(raw: Any, index: int) -> RuleSpec:
    if not isinstance(raw, Mapping):
        raise SpecError(f"rules[{index}] must be a mapping")
    rid = raw.get("id")
    if not isinstance(rid, str) or not RULE_ID_RE.match(rid):
        raise SpecError(f"rules[{index}]: invalid id {rid!r}")
    has_formula = "formula" in raw
    has_template = "template" in raw
    if has_formula == has_template:
        raise SpecError(f"rule {rid}: exactly one of 'formula' or 'template' is required")
    template_args = {k: v for k, v in raw.items() if k not in ("id", "name", "description", "formula", "template", "on_violation", "alert")}
    if has_formula:
        if template_args:
            raise SpecError(f"rule {rid}: unknown keys {sorted(template_args)}")
        formula = raw["formula"]
        if not isinstance(formula, str) or not formula.strip():
            raise SpecError(f"rule {rid}: formula must be a non-empty string")
        formula = formula.strip()
        template = None
    else:
        template = raw["template"]
        expected = TEMPLATES.get(template)
        if expected is None:
            raise SpecError(f"rule {rid}: unknown template {template!r}")
        if set(template_args) != set(expected):
            raise SpecError(f"rule {rid}: template {template} takes {list(expected)}, got {sorted(template_args)}")
        try:
            formula = expand_template(template, template_args)
        except SpecError as e:
            raise SpecError(f"rule {rid}: {e}") from None
    for p in formula_props(formula):
        if not PROP_NAME_RE.match(p) or p in RESERVED_NAMES:
            raise SpecError(f"rule {rid}: invalid proposition name {p!r} (use lowercase snake_case)")
    on_violation = raw.get("on_violation", "deny")
    if on_violation not in ON_VIOLATION_VALUES:
        raise SpecError(f"rule {rid}: on_violation must be one of {ON_VIOLATION_VALUES}")
    alert = raw.get("alert")
    if alert not in ALERT_VALUES:
        raise SpecError(f"rule {rid}: alert must be one of {ALERT_VALUES}")
    name = raw.get("name", "")
    description = raw.get("description", "")
    if not isinstance(name, str) or not isinstance(description, str):
        raise SpecError(f"rule {rid}: name and description must be strings")
    return RuleSpec(
        id=rid,
        name=name.strip(),
        description=" ".join(description.split()),
        formula=formula,
        on_violation=on_violation,
        alert=alert,
        source_template=template,
    )


def parse_spec(data: Any) -> Spec:
    if not isinstance(data, Mapping):
        raise SpecError("rule file must be a mapping")
    if data.get("version") != 1:
        raise SpecError("version must be 1")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise SpecError("rules must be a non-empty list")
    rules = tuple(_parse_rule(r, i) for i, r in enumerate(raw_rules))
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise SpecError("duplicate rule ids")
    return Spec(version=1, rules=rules)


def load_spec(path: str | Path) -> Spec:
    with open(path, encoding="utf-8") as f:
        return parse_spec(yaml.safe_load(f))
