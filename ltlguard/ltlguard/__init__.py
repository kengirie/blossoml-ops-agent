"""ltlguard: LTLf rules over agent tool calls.

The runtime (``ltlguard.core``, ``ltlguard.spec``, ``ltlguard.trace``) is pure
Python. Spot is imported only by ``ltlguard.compile``.
"""

__version__ = "0.1.0"

from ltlguard.core.dfa import CompiledRule, RuleSet, load_compiled
from ltlguard.core.labeling import Candidate, Labeler
from ltlguard.core.monitor import Monitor, PreviewResult, Verdict

__all__ = [
    "Candidate",
    "CompiledRule",
    "Labeler",
    "Monitor",
    "PreviewResult",
    "RuleSet",
    "Verdict",
    "load_compiled",
]
