import asyncio

import pytest

from conftest import FIXTURE_RULES
from ltlguard.adapters.claude_agent_sdk.hooks import ClaudeAgentSdkAdapter
from ltlguard.core.automaton import load_rules
from ltlguard.core.monitor import Monitor
from ltlguard.core.session import GuardSession
from test_session import FakeLabeler


class T:
    session = "t"

    def write(self, *a, **k):
        return {}


def adapter(**kw):
    return ClaudeAgentSdkAdapter(GuardSession(Monitor(load_rules(FIXTURE_RULES)), FakeLabeler(), T()), **kw)


def pre(name, tid):
    return {"tool_name": name, "tool_input": {}, "tool_use_id": tid}


async def test_parallel_call_waits_for_the_previous_one():
    a = adapter()
    assert await a.pre_tool_use(pre("status", "t1"), "t1", None) == {}
    second = asyncio.create_task(a.pre_tool_use(pre("logs", "t2"), "t2", None))
    await asyncio.sleep(0.2)
    assert not second.done()  # waiting, not denied
    await a.post_tool_use({"tool_name": "status", "tool_use_id": "t1"}, "t1", None)
    assert await asyncio.wait_for(second, 2) == {}
    assert a.waited == 1


async def test_parallel_call_denied_after_the_wait_limit():
    a = adapter(hook_timeout=5, internal_timeout=1, serialize_wait=0.3)
    await a.pre_tool_use(pre("status", "t1"), "t1", None)
    out = await a.pre_tool_use(pre("logs", "t2"), "t2", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "1 つずつ" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_timeouts_must_nest():
    with pytest.raises(ValueError):
        adapter(hook_timeout=10, internal_timeout=5, serialize_wait=5)
