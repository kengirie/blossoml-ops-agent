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

Formulas are parsed by Spot (LTLf syntax: ``!``, ``&``, ``|``, ``->``, ``<->``,
``X`` (weak next), ``X[!]`` (strong next), ``F``, ``G``, ``U``, ``W``, ``R``,
``M``). Template arguments are Boolean formulas, also parsed by Spot, and
templates are built with Spot's formula constructors. Proposition names are
lowercase identifiers; their meaning is defined by the labeling function.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import spot
import yaml

ON_VIOLATION_VALUES = ("deny", "log_only")
ALERT_VALUES = (None, "info", "warning", "critical")
RULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_'.-]{0,31}$")
PROP_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

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


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class RuleSpec:
    id: str
    name: str
    description: str
    formula: "spot.formula"
    on_violation: str
    alert: str | None
    source_template: str | None = None


@dataclass(frozen=True)
class Spec:
    version: int
    rules: tuple[RuleSpec, ...]
    source_sha256: str


def parse_formula(text: str) -> "spot.formula":
    """Parse an LTLf formula with Spot."""
    if not isinstance(text, str) or not text.strip():
        raise SpecError("formula must be a non-empty string")
    parsed = spot.parse_infix_psl(text)
    if parsed.errors:
        raise SpecError(f"cannot parse formula {text!r}")
    f = parsed.f
    if not f.is_ltl_formula():
        raise SpecError(f"not an LTL formula: {text!r}")
    return f


def _boolean_arg(name: str, value: Any) -> "spot.formula":
    f = parse_formula(value) if isinstance(value, str) else None
    if f is None or not f.is_boolean():
        raise SpecError(f"template argument {name!r} must be a Boolean formula over propositions")
    return f


def expand_template(template: str, args: Mapping[str, Any]) -> "spot.formula":
    F = spot.formula
    if template not in TEMPLATES:
        raise SpecError(f"unknown template {template!r} (known: {', '.join(sorted(TEMPLATES))})")
    if template == "always":
        return F.G(_boolean_arg("prop", args.get("prop")))
    if template == "never":
        return F.G(F.Not(_boolean_arg("prop", args.get("prop"))))
    if template == "eventually":
        return F.F(_boolean_arg("prop", args.get("prop")))
    if template == "not_until":
        a = _boolean_arg("prop", args.get("prop"))
        b = _boolean_arg("until", args.get("until"))
        return F.W(F.Not(a), b)
    if template == "response":
        a = _boolean_arg("trigger", args.get("trigger"))
        b = _boolean_arg("response", args.get("response"))
        return F.G(F.Implies(a, F.F(b)))
    if template == "at_most_once":
        return expand_template("at_most", {"prop": args.get("prop"), "count": 1})
    if template == "at_most":
        a = _boolean_arg("prop", args.get("prop"))
        n = args.get("count")
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= _MAX_AT_MOST:
            raise SpecError(f"at_most: count must be an integer in 0..{_MAX_AT_MOST}")
        # f_0 = G !a ; f_k = G(a -> X f_{k-1})   (X is the weak next of LTLf)
        f = F.G(F.Not(a))
        for _ in range(n):
            f = F.G(F.Implies(a, F.X(f)))
        return f
    raise AssertionError(template)


def atomic_props(f: "spot.formula") -> frozenset[str]:
    return frozenset(str(ap) for ap in spot.atomic_prop_collect(f))


_RULE_KEYS = ("id", "name", "description", "formula", "template", "on_violation", "alert")


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
    template_args = {k: v for k, v in raw.items() if k not in _RULE_KEYS}
    try:
        if has_formula:
            if template_args:
                raise SpecError(f"unknown keys {sorted(template_args)}")
            formula = parse_formula(raw["formula"])
            template = None
        else:
            template = raw["template"]
            expected = TEMPLATES.get(template)
            if expected is None:
                raise SpecError(f"unknown template {template!r}")
            if set(template_args) != set(expected):
                raise SpecError(f"template {template} takes {list(expected)}, got {sorted(template_args)}")
            formula = expand_template(template, template_args)
    except SpecError as e:
        raise SpecError(f"rule {rid}: {e}") from None
    for p in atomic_props(formula):
        if not PROP_NAME_RE.match(p):
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


def parse_spec(data: Any, source_sha256: str = "") -> Spec:
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
    return Spec(version=1, rules=rules, source_sha256=source_sha256)


def load_spec(path: str | Path) -> Spec:
    try:
        source = Path(path).read_bytes()
        data = yaml.safe_load(source.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise SpecError(f"cannot read {path}: {e}") from None
    return parse_spec(data, hashlib.sha256(source).hexdigest())
