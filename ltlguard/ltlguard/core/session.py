"""A guarded session: ties a Monitor, a Labeler and a trace together.

This is framework-independent. Adapters (e.g. for the Claude Agent SDK) call:

- ``before_call``  at PreToolUse  -> allow, or deny with a message
- ``after_call``   at PostToolUse / PostToolUseFailure
- ``at_stop``      when the agent wants to finish -> None, or a message that
                   sends it back to fulfil pending obligations

Monitored calls are serialized: while one call is in flight, another
``before_call`` is denied (the agent is asked to call tools one at a time).

``require_cleared`` lets tool implementations double-check, from inside the
tool, that the call was cleared by ``before_call``. If a hook failed open for
any reason (timeout, misconfiguration), the tool still refuses to run.

Every internal error is treated as a denial (fail-closed). If the monitor
state can no longer be trusted (an error while committing), the session is
marked broken and denies every later call.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ltlguard.core.automaton import Rule
from ltlguard.core.labeling import Labeler
from ltlguard.core.monitor import Monitor
from ltlguard.trace.jsonl import TraceWriter

AlertCallback = Callable[[str, str, str], None]  # (severity, title, body)
# Returns what may be stored in the trace for a call's arguments (e.g. after a content check).
ArgsFilter = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class NotCleared(Exception):
    """Raised by ``require_cleared`` when a tool runs without a matching cleared call."""


def canonical_args(args: Mapping[str, Any] | None) -> str:
    return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


@dataclass
class Decision:
    allow: bool
    message: str = ""
    rules: list[str] = field(default_factory=list)


@dataclass
class _InFlight:
    tool: str
    args: Mapping[str, Any]
    args_canon: str
    call_id: str | None
    call_props: frozenset[str]
    started: float
    result: Mapping[str, Any] | None = None
    result_ok: bool = False
    result_recorded: bool = False
    executions: int = 0


class GuardSession:
    def __init__(
        self,
        monitor: Monitor,
        labeler: Labeler,
        trace: TraceWriter,
        *,
        on_alert: AlertCallback | None = None,
        max_stop_reprompts: int = 2,
        inflight_timeout: float = 900.0,
        state_path: str | None = None,
        args_filter: ArgsFilter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.monitor = monitor
        self.labeler = labeler
        self.trace = trace
        self.on_alert = on_alert
        self.max_stop_reprompts = max_stop_reprompts
        self.inflight_timeout = inflight_timeout
        self.state_path = state_path
        self.args_filter = args_filter
        self.clock = clock
        self.broken: str | None = None
        self.stop_reprompts = 0
        self.unmet_at_stop: list[str] = []
        self.denials = 0
        self._inflight: _InFlight | None = None

    # ---- helpers -------------------------------------------------------

    def _alert(self, severity: str, title: str, body: str) -> None:
        if self.on_alert is None:
            return
        try:
            self.on_alert(severity, title, body)
        except Exception as e:  # never let alerting break enforcement
            self.trace.write("alert_error", error=type(e).__name__)

    def _alert_rules(self, rules: list[Rule], what: str, tool: str) -> None:
        for r in rules:
            if r.alert:
                self._alert(
                    r.alert,
                    f"ルール {r.id} が発火しました",
                    f"ルール {r.id}「{r.name}」: {what}（ツール: {tool}）。"
                    "通常は発火しないはずのルールです。設定またはツール実装の不具合を疑ってください。",
                )

    def allowed_operations(self) -> list[str]:
        cands = []
        for c in self.labeler.candidates():
            cands.append((c, self.labeler.call_props(c.tool, c.args), self.labeler.possible_result_props(c.tool, c.args)))
        return self.monitor.allowed_candidates(cands)

    def deny_message(self, rules: list[Rule]) -> str:
        lines = []
        for r in rules:
            lines.append(f"ルール {r.id}「{r.name}」に違反するため実行していません。")
            if r.description:
                lines.append(f"理由: {r.description}")
        try:
            ops = self.allowed_operations()
            lines.append("現在実行できる操作: " + (", ".join(ops) if ops else "なし"))
        except Exception:
            pass
        return "\n".join(lines)

    def _save_state(self) -> None:
        if not self.state_path:
            return
        import os
        import tempfile

        data = json.dumps({"session": self.trace.session, **self.monitor.snapshot()}, ensure_ascii=False)
        d = os.path.dirname(self.state_path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".monitor-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.state_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _traceable_args(self, args: Mapping[str, Any]) -> Any:
        if self.args_filter is None:
            return dict(args)
        try:
            return self.args_filter(args)
        except Exception:
            return {"_omitted": True}

    # ---- PreToolUse ----------------------------------------------------

    def before_call(self, tool: str, args: Mapping[str, Any] | None, call_id: str | None = None) -> Decision:
        try:
            return self._before_call(tool, dict(args or {}), call_id)
        except Exception as e:
            self.denials += 1
            self.trace.write("deny", tool=tool, call_id=call_id, reason="internal_error", error=type(e).__name__)
            return Decision(False, "ガードの内部エラーのため実行していません（fail-closed）。")

    def _before_call(self, tool: str, args: dict, call_id: str | None) -> Decision:
        if self.broken:
            self.denials += 1
            self.trace.write("deny", tool=tool, call_id=call_id, reason="monitor_broken")
            return Decision(False, "監視の状態が信頼できないため、これ以上の操作は実行できません。作業を終了してください。")

        inf = self._inflight
        if inf is not None:
            if self.clock() - inf.started > self.inflight_timeout:
                # The previous call never completed. Count it as executed (safe side).
                self.trace.write("stale_inflight", tool=inf.tool, call_id=inf.call_id)
                self._commit(inf, None)
            else:
                self.denials += 1
                self.trace.write("deny", tool=tool, call_id=call_id, reason="concurrent_call")
                return Decision(False, "別の操作の実行中です。ツールは 1 つずつ、前の結果を受け取ってから呼び出してください。")

        call_props = frozenset(self.labeler.call_props(tool, args))
        possible = frozenset(self.labeler.possible_result_props(tool, args))
        preview = self.monitor.preview(call_props, possible)
        if preview.shadow_denied_by:
            self.trace.write(
                "shadow_deny", tool=tool, call_id=call_id, props=sorted(call_props),
                rules=[r.id for r in preview.shadow_denied_by],
            )
        if not preview.allowed:
            self.denials += 1
            msg = self.deny_message(preview.denied_by)
            self.trace.write(
                "deny", tool=tool, call_id=call_id, args=self._traceable_args(args), props=sorted(call_props),
                rules=[r.id for r in preview.denied_by], monitor=self.monitor.describe(),
            )
            self._alert_rules(preview.denied_by, "違反する呼び出しを拒否しました", tool)
            return Decision(False, msg, [r.id for r in preview.denied_by])

        self._inflight = _InFlight(tool, args, canonical_args(args), call_id, call_props, self.clock())
        self.trace.write("pre", tool=tool, call_id=call_id, args=self._traceable_args(args), props=sorted(call_props))
        return Decision(True)

    # ---- inside the tool -----------------------------------------------

    def require_cleared(self, tool: str, args: Mapping[str, Any] | None) -> None:
        inf = self._inflight
        if self.broken or inf is None or inf.tool != tool or inf.args_canon != canonical_args(args) or inf.executions:
            self.trace.write("not_cleared", tool=tool)
            self._alert("critical", "ガードを通っていないツール実行", f"{tool} が事前チェックなしで呼ばれたため実行を拒否しました。")
            raise NotCleared(tool)
        inf.executions += 1

    def record_result(self, tool: str, result: Mapping[str, Any] | None, ok: bool) -> None:
        """Called by the tool with its checked, structured result (or None on failure)."""
        inf = self._inflight
        if inf is None or inf.tool != tool:
            return
        inf.result = result
        inf.result_ok = ok
        inf.result_recorded = True

    # ---- PostToolUse ---------------------------------------------------

    def after_call(self, tool: str, call_id: str | None, failed: bool = False) -> None:
        inf = self._inflight
        if inf is None or inf.tool != tool or (call_id and inf.call_id and inf.call_id != call_id):
            # A post without a matching pre (e.g. a call that was not monitored).
            self.trace.write("post_unmatched", tool=tool, call_id=call_id)
            return
        result = inf.result if (inf.result_recorded and inf.result_ok and not failed) else None
        self._commit(inf, result)

    def _commit(self, inf: _InFlight, result: Mapping[str, Any] | None) -> None:
        self._inflight = None
        try:
            props = set(inf.call_props)
            if result is not None:
                props |= self.labeler.result_props(inf.tool, inf.args, result)
            newly = self.monitor.commit(props)
            self.trace.write(
                "post", tool=inf.tool, call_id=inf.call_id, props=sorted(props),
                ok=result is not None, result_summary=result, monitor=self.monitor.describe(),
            )
            if newly:
                self.trace.write("violation", tool=inf.tool, rules=[r.id for r in newly])
                self._alert_rules(newly, "違反が確定しました", inf.tool)
            self._save_state()
        except Exception as e:
            self.broken = f"commit failed: {type(e).__name__}"
            self.trace.write("monitor_broken", tool=inf.tool, error=type(e).__name__)
            self._alert("critical", "LTL モニタの異常", f"状態の更新に失敗しました（{type(e).__name__}）。以降の操作はすべて拒否されます。")

    # ---- external events (e.g. a human action) -------------------------

    def record_external(self, kind: str, props: frozenset[str], detail: Mapping[str, Any] | None = None) -> None:
        if self._inflight is not None:
            raise RuntimeError("cannot record an external event while a call is in flight")
        newly = self.monitor.commit(props)
        self.trace.write(kind, props=sorted(props), detail=dict(detail or {}), monitor=self.monitor.describe())
        if newly:
            self._alert_rules(newly, "違反が確定しました", kind)
        self._save_state()

    # ---- Stop ------------------------------------------------------------

    def obligations_message(self, rules: list[Rule]) -> str:
        lines = ["まだ終了できません。次のルールの義務が未履行です。"]
        for r in rules:
            lines.append(f"- ルール {r.id}「{r.name}」: {r.description}")
        lines.append("必要な操作を行ってから終了してください。")
        return "\n".join(lines)

    def at_stop(self) -> str | None:
        """Return a message to send the agent back, or None to let it stop."""
        try:
            pending = self.monitor.pending_obligations()
        except Exception as e:
            self.broken = f"stop check failed: {type(e).__name__}"
            self.trace.write("stop", outcome="error", error=type(e).__name__)
            return None
        if not pending:
            self.trace.write("stop", outcome="ok", monitor=self.monitor.describe())
            return None
        if self.broken is None and self.stop_reprompts < self.max_stop_reprompts:
            self.stop_reprompts += 1
            self.trace.write("stop", outcome="reprompt", attempt=self.stop_reprompts, rules=[r.id for r in pending])
            return self.obligations_message(pending)
        self.unmet_at_stop = [r.id for r in pending]
        self.trace.write("stop", outcome="unmet", rules=self.unmet_at_stop, monitor=self.monitor.describe())
        return None

    @property
    def in_flight(self) -> bool:
        return self._inflight is not None
