"""ltlguard: LTLf rules over agent tool calls.

Rules are parsed and translated to automata by Spot (>= 2.14, with Python
bindings), which is also used at runtime to step the automata.
"""

__version__ = "0.1.0"

from ltlguard.core.automaton import Rule, RuleSet, RulesError, RuleState, load_rules
from ltlguard.core.labeling import Candidate, Labeler
from ltlguard.core.monitor import Monitor, PreviewResult, Verdict

__all__ = [
    "Candidate",
    "Labeler",
    "Monitor",
    "PreviewResult",
    "Rule",
    "RuleSet",
    "RuleState",
    "RulesError",
    "Verdict",
    "load_rules",
]
