"""Boolean guard expressions over atomic propositions.

Grammar (whitespace is ignored)::

    expr  := conj ('|' conj)*
    conj  := unary ('&' unary)*
    unary := '!' unary | '(' expr ')' | atom
    atom  := identifier | 'true' | 'false' | '1' | '0'

Expressions are parsed once into a small tuple AST and evaluated against the
set of propositions that are true at a step.
"""

from __future__ import annotations

import re
from typing import AbstractSet, Tuple, Union

# AST nodes:
#   ("const", bool) | ("ap", name) | ("not", node) | ("and", (node, ...)) | ("or", (node, ...))
Node = Tuple[str, Union[bool, str, "Node", Tuple["Node", ...]]]

PROP_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
RESERVED_NAMES = frozenset({"true", "false"})

_TOKEN_RE = re.compile(r"\s*(?:([()!&|])|([A-Za-z_][A-Za-z0-9_]*|[01]))")


class GuardSyntaxError(ValueError):
    pass


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise GuardSyntaxError(f"unexpected character at {pos}: {text[pos:pos + 10]!r}")
        tokens.append(m.group(1) or m.group(2))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> str:
        tok = self.peek()
        if tok is None:
            raise GuardSyntaxError("unexpected end of expression")
        self.i += 1
        return tok

    def expr(self) -> Node:
        items = [self.conj()]
        while self.peek() == "|":
            self.take()
            items.append(self.conj())
        return items[0] if len(items) == 1 else ("or", tuple(items))

    def conj(self) -> Node:
        items = [self.unary()]
        while self.peek() == "&":
            self.take()
            items.append(self.unary())
        return items[0] if len(items) == 1 else ("and", tuple(items))

    def unary(self) -> Node:
        tok = self.take()
        if tok == "!":
            return ("not", self.unary())
        if tok == "(":
            node = self.expr()
            if self.take() != ")":
                raise GuardSyntaxError("expected ')'")
            return node
        if tok in ("true", "1"):
            return ("const", True)
        if tok in ("false", "0"):
            return ("const", False)
        if PROP_NAME_RE.match(tok):
            return ("ap", tok)
        raise GuardSyntaxError(f"unexpected token {tok!r}")


def parse(text: str) -> Node:
    tokens = _tokenize(text)
    if not tokens:
        raise GuardSyntaxError("empty expression")
    parser = _Parser(tokens)
    node = parser.expr()
    if parser.peek() is not None:
        raise GuardSyntaxError(f"trailing token {parser.peek()!r}")
    return node


def evaluate(node: Node, true_props: AbstractSet[str]) -> bool:
    kind = node[0]
    if kind == "const":
        return bool(node[1])
    if kind == "ap":
        return node[1] in true_props
    if kind == "not":
        return not evaluate(node[1], true_props)  # type: ignore[arg-type]
    if kind == "and":
        return all(evaluate(c, true_props) for c in node[1])  # type: ignore[union-attr]
    if kind == "or":
        return any(evaluate(c, true_props) for c in node[1])  # type: ignore[union-attr]
    raise GuardSyntaxError(f"unknown node kind {kind!r}")


def props_of(node: Node) -> frozenset[str]:
    kind = node[0]
    if kind == "ap":
        return frozenset([node[1]])  # type: ignore[list-item]
    if kind == "not":
        return props_of(node[1])  # type: ignore[arg-type]
    if kind in ("and", "or"):
        out: frozenset[str] = frozenset()
        for c in node[1]:  # type: ignore[union-attr]
            out |= props_of(c)
        return out
    return frozenset()


class Guard:
    """A parsed guard with its source text."""

    __slots__ = ("text", "node")

    def __init__(self, text: str):
        self.text = text
        self.node = parse(text)

    def __call__(self, true_props: AbstractSet[str]) -> bool:
        return evaluate(self.node, true_props)

    def __repr__(self) -> str:
        return f"Guard({self.text!r})"
