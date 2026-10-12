"""Claude Agent SDK hooks for a GuardSession.

The only module in ltlguard that depends on ``claude_agent_sdk``.

Every hook body runs under an internal timeout shorter than the SDK hook
timeout, and any exception or timeout in PreToolUse becomes a denial: the SDK
treats a failing hook as non-blocking, which would otherwise fail open.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ltlguard.core.session import GuardSession


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class ClaudeAgentSdkAdapter:
    def __init__(self, session: GuardSession, *, hook_timeout: float = 20.0, internal_timeout: float = 10.0):
        if internal_timeout >= hook_timeout:
            raise ValueError("internal_timeout must be shorter than hook_timeout")
        self.session = session
        self.hook_timeout = hook_timeout
        self.internal_timeout = internal_timeout
        # Serializes hook bodies (they touch shared monitor state).
        self._lock = asyncio.Lock()

    async def _run(self, fn, *args):
        async def locked():
            async with self._lock:
                return fn(*args)

        return await asyncio.wait_for(locked(), timeout=self.internal_timeout)

    async def pre_tool_use(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        tool = str(input_data.get("tool_name", ""))
        try:
            args = input_data.get("tool_input") or {}
            if not isinstance(args, dict):
                return _deny("引数の形式が不正です。")
            decision = await self._run(self.session.before_call, tool, args, tool_use_id or input_data.get("tool_use_id"))
        except Exception as e:
            try:
                self.session.trace.write("deny", tool=tool, reason="hook_error", error=type(e).__name__)
            except Exception:
                pass
            return _deny("ガードの内部エラーまたはタイムアウトのため実行していません（fail-closed）。")
        if decision.allow:
            return {}
        return _deny(decision.message)

    async def post_tool_use(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        await self._post(input_data, tool_use_id, failed=False)
        return {}

    async def post_tool_use_failure(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        await self._post(input_data, tool_use_id, failed=True)
        return {}

    async def _post(self, input_data: dict, tool_use_id: str | None, failed: bool) -> None:
        tool = str(input_data.get("tool_name", ""))
        try:
            await self._run(self.session.after_call, tool, tool_use_id or input_data.get("tool_use_id"), failed)
        except Exception as e:
            self.session.broken = f"post hook failed: {type(e).__name__}"
            try:
                self.session.trace.write("monitor_broken", tool=tool, error=type(e).__name__)
            except Exception:
                pass

    async def stop(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        try:
            message = await self._run(self.session.at_stop)
        except Exception:
            return {}
        if message:
            return {"decision": "block", "reason": message}
        return {}

    def hooks(self) -> dict:
        """The ``hooks`` value for ``ClaudeAgentOptions``."""
        from claude_agent_sdk import HookMatcher

        t = self.hook_timeout
        return {
            "PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use], timeout=t)],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[self.post_tool_use], timeout=t)],
            "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[self.post_tool_use_failure], timeout=t)],
            "Stop": [HookMatcher(matcher=None, hooks=[self.stop], timeout=t)],
        }
