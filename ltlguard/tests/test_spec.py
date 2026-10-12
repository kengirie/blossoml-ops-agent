import pytest
import spot

from ltlguard.spec.loader import SpecError, expand_template, parse_spec


@pytest.mark.parametrize(
    "template,args,expected",
    [
        ("always", {"prop": "a"}, "G a"),
        ("never", {"prop": "a"}, "G !a"),
        ("not_until", {"prop": "a", "until": "b"}, "!a W b"),
        ("response", {"trigger": "a | b", "response": "c"}, "G((a | b) -> F c)"),
        ("at_most_once", {"prop": "a"}, "G(a -> X G !a)"),
        ("at_most", {"prop": "a", "count": 2}, "G(a -> X G(a -> X G !a))"),
        ("at_most", {"prop": "a", "count": 0}, "G !a"),
        ("eventually", {"prop": "a & !b"}, "F(a & !b)"),
    ],
)
def test_templates(template, args, expected):
    assert expand_template(template, args) == spot.formula(expected)


def test_parse_spec_ok():
    spec = parse_spec(
        {
            "version": 1,
            "rules": [
                {"id": "R1", "name": "n", "description": "d", "formula": "G a", "on_violation": "deny", "alert": "critical"},
                {"id": "R2", "template": "response", "trigger": "x", "response": "y", "on_violation": "log_only"},
            ],
        }
    )
    assert [r.id for r in spec.rules] == ["R1", "R2"]
    assert spec.rules[1].formula == spot.formula("G(x -> F y)")
    assert spec.rules[1].on_violation == "log_only"


@pytest.mark.parametrize(
    "rule",
    [
        {"id": "R1"},  # neither formula nor template
        {"id": "R1", "formula": "G a", "template": "never", "prop": "a"},
        {"id": "R1", "formula": "G a", "on_violation": "ask_human"},
        {"id": "R1", "formula": "G Abc"},  # uppercase proposition
        {"id": "R1", "template": "never"},  # missing arg
        {"id": "R1", "template": "never", "prop": "a; rm"},
        {"id": "R1", "template": "never", "prop": "F a"},  # template arguments must be Boolean
        {"id": "R1", "formula": "G (a"},  # Spot parse error
        {"id": "R1", "formula": "{a;b}[]-> c"},  # PSL, not LTL
        {"id": "R1", "template": "at_most", "prop": "a", "count": -1},
        {"id": "R1", "formula": "G a", "extra": 1},
        {"id": "1bad", "formula": "G a"},
    ],
)
def test_parse_spec_errors(rule):
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "rules": [rule]})


def test_duplicate_ids():
    with pytest.raises(SpecError):
        parse_spec({"version": 1, "rules": [{"id": "R1", "formula": "G a"}, {"id": "R1", "formula": "G b"}]})
