"""The labeling interface: maps tool-call events to sets of propositions.

The user of ltlguard implements this. ltlguard never interprets tool names or
arguments itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class Candidate:
    """A representative call, used to list "operations you can run now"."""

    label: str
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)


class Labeler(Protocol):
    def call_props(self, tool: str, args: Mapping[str, Any]) -> frozenset[str]:
        """Propositions decided by the tool name and arguments alone (known at PreToolUse)."""

    def possible_result_props(self, tool: str, args: Mapping[str, Any]) -> frozenset[str]:
        """Result propositions this call *could* make true. Used at PreToolUse,
        where results are unknown: a call is denied only if no subset of these
        avoids a violation."""

    def result_props(self, tool: str, args: Mapping[str, Any], result: Mapping[str, Any] | None) -> frozenset[str]:
        """Propositions decided by the result (known at PostToolUse). ``result``
        is None when the tool failed."""

    def all_props(self) -> frozenset[str]:
        """Every proposition this labeler can produce. Used to check that the
        rules do not mention a proposition that can never become true."""

    def candidates(self) -> list[Candidate]:
        """Representative calls for the "currently allowed operations" list."""
