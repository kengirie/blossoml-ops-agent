import pytest

from ltlguard.spec.loader import SpecError, expand_template, parse_spec


def test_templates():
    assert expand_template("never", {"prop": "a"}) == "G!(a)"
    assert expand_template("not_until", {"prop": "a", "until": "b"}) == "!(a) W (b)"
    assert expand_template("response", {"trigger": "a | b", "response": "c"}) == "G((a | b) -> F(c))"
    assert expand_template("at_most_once", {"prop": "a"}) == "G((a) -> X(G!(a)))"
    assert expand_template("eventually", {"prop": "a"}) == "F(a)"
    assert expand_template("at_most", {"prop": "a", "count": 0}) == "G!(a)"


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
    assert spec.rules[1].formula == "G((x) -> F(y))"
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
