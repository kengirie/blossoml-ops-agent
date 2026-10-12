import importlib.util

import pytest

HAS_SPOT = importlib.util.find_spec("spot") is not None

requires_spot = pytest.mark.skipif(not HAS_SPOT, reason="Spot is not installed")


def rule_from_automaton(rule_id: str, formula: str, automaton: dict, on_violation: str = "deny", alert=None):
    from ltlguard.core.dfa import _parse_rule

    return _parse_rule(
        {
            "id": rule_id,
            "name": rule_id,
            "description": f"desc {rule_id}",
            "formula": formula,
            "on_violation": on_violation,
            "alert": alert,
            **automaton,
        }
    )
