"""Common machinery for blossoml-ops tools.

Every tool call goes through ``ToolRunner.invoke``:

1. ``require_cleared``: the call must have been cleared by the LTL guard at
   PreToolUse (fails closed if a hook did not run).
2. Strict argument validation: enums, bounded integers, length-limited
   strings, no unknown keys.
3. The tool body (blocking work runs in a thread, with a timeout).
4. The output guard (``blossoml_ops.guard``). On violation the result is
   discarded, Claude gets only the kind of violation, and a critical alert is
   raised.
5. The checked result is handed to the guard session (for result
   propositions and the trace).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from blossoml_ops.guard import GuardViolation, OutputTooLarge, check_output, check_string
from blossoml_ops.labeling import full_name


class ArgError(ValueError):
    pass


class ToolError(Exception):
    """An expected failure with a fixed, safe message for Claude."""


@dataclass(frozen=True)
class Param:
    kind: str  # "enum" | "int" | "int_choice" | "str"
    description: str
    choices: tuple = ()
    minimum: int = 0
    maximum: int = 0
    max_len: int = 0
    required: bool = True

    def json_schema(self) -> dict:
        if self.kind == "enum":
            return {"type": "string", "enum": list(self.choices), "description": self.description}
        if self.kind == "int":
            return {"type": "integer", "minimum": self.minimum, "maximum": self.maximum, "description": self.description}
        if self.kind == "int_choice":
            return {"type": "integer", "enum": list(self.choices), "description": self.description}
        if self.kind == "str":
            return {"type": "string", "minLength": 1, "maxLength": self.max_len, "description": self.description}
        raise ValueError(self.kind)

    def validate(self, name: str, value: Any) -> Any:
        if self.kind == "enum":
            if not isinstance(value, str) or value not in self.choices:
                raise ArgError(f"{name} must be one of {list(self.choices)}")
            return value
        if self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ArgError(f"{name} must be an integer")
            if not self.minimum <= value <= self.maximum:
                raise ArgError(f"{name} must be in {self.minimum}..{self.maximum}")
            return value
        if self.kind == "int_choice":
            if isinstance(value, bool) or not isinstance(value, int) or value not in self.choices:
                raise ArgError(f"{name} must be one of {list(self.choices)}")
            return value
        if self.kind == "str":
            if not isinstance(value, str) or not value.strip():
                raise ArgError(f"{name} must be a non-empty string")
            if len(value) > self.max_len:
                raise ArgError(f"{name} must be at most {self.max_len} characters")
            try:
                check_string(value)
            except GuardViolation as e:
                raise ArgError(f"{name} contains disallowed content ({e.kind})") from None
            return value
        raise ArgError(name)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: Mapping[str, Param]
    run: Callable[..., Any]  # run(ctx, **args) -> dict ; may be async
    timeout_s: float = 60.0
    degrade: Callable[[dict], dict] | None = None
    representative_args: list[dict] = field(default_factory=list)
    read_only: bool = True

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {k: p.json_schema() for k, p in self.params.items()},
            "required": [k for k, p in self.params.items() if p.required],
            "additionalProperties": False,
        }

    def validate(self, args: Any) -> dict:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ArgError("arguments must be an object")
        unknown = set(args) - set(self.params)
        if unknown:
            raise ArgError(f"unknown arguments: {sorted(unknown)}")
        out = {}
        for k, p in self.params.items():
            if k not in args:
                if p.required:
                    raise ArgError(f"missing argument {k}")
                continue
            out[k] = p.validate(k, args[k])
        return out


def _text(payload: dict, is_error: bool = False) -> dict:
    out: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
    if is_error:
        out["is_error"] = True
    return out


class ToolRunner:
    def __init__(self, ctx):
        self.ctx = ctx

    async def invoke(self, spec: ToolSpec, raw_args: Any) -> dict:
        ctx = self.ctx
        name = full_name(spec.name)
        session = ctx.session
        try:
            session.require_cleared(name, raw_args if isinstance(raw_args, dict) else {})
        except Exception:
            return _text({"error": "not_cleared", "message": "この呼び出しは事前チェックを通っていないため実行していません。"}, True)

        try:
            args = spec.validate(raw_args)
        except ArgError as e:
            session.record_result(name, None, False)
            return _text({"error": "invalid_arguments", "message": str(e)}, True)

        try:
            if inspect.iscoroutinefunction(spec.run):
                result = await asyncio.wait_for(spec.run(ctx, **args), spec.timeout_s)
            else:
                result = await asyncio.wait_for(asyncio.to_thread(spec.run, ctx, **args), spec.timeout_s)
        except ToolError as e:
            result = {"error": "tool_failed", "message": str(e)}
        except asyncio.TimeoutError:
            result = {"error": "timeout"}
        except Exception as e:
            # The exception text may contain anything; only the type is reported.
            result = {"error": "tool_failed", "type": type(e).__name__}

        try:
            try:
                text = check_output(result)
            except OutputTooLarge:
                if spec.degrade is None:
                    raise
                result = spec.degrade(result)
                text = check_output(result)
        except GuardViolation as e:
            session.record_result(name, None, False)
            ctx.trace.write("guard_violation", tool=name, kind=e.kind)
            ctx.alert(
                "critical",
                "出口検査でツール出力を破棄しました",
                f"ツール {spec.name} の出力が出口検査に違反したため破棄しました（種類: {e.kind}）。中身は記録していません。",
            )
            return _text({"error": "output_discarded", "reason_kind": e.kind}, True)

        is_error = "error" in result
        session.record_result(name, result, not is_error)
        out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            out["is_error"] = True
        return out
