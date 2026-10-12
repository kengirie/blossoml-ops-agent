import pytest

from ltlguard.core.guard_expr import Guard, GuardSyntaxError, parse, props_of


@pytest.mark.parametrize(
    "text,true_props,expected",
    [
        ("true", set(), True),
        ("false", set(), False),
        ("1", set(), True),
        ("0", {"a"}, False),
        ("a", {"a"}, True),
        ("a", set(), False),
        ("!a", set(), True),
        ("a & b", {"a"}, False),
        ("a & b", {"a", "b"}, True),
        ("a | b", {"b"}, True),
        ("!a & (b | c)", {"c"}, True),
        ("!a & (b | c)", {"a", "c"}, False),
        ("!(a | b)", set(), True),
        ("a | b & c", {"a"}, True),  # & binds tighter
        ("(a | b) & c", {"a"}, False),
    ],
)
def test_evaluate(text, true_props, expected):
    assert Guard(text)(true_props) is expected


@pytest.mark.parametrize("text", ["", "a &", "(a", "a b", "A", "a + b", "a)", "!"])
def test_syntax_errors(text):
    with pytest.raises(GuardSyntaxError):
        parse(text)


def test_props_of():
    assert props_of(parse("!a & (b | true)")) == {"a", "b"}
