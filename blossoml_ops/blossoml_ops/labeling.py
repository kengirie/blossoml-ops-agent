"""Labeling function: tool calls of blossoml-ops -> propositions used in rules.yaml.

Call-time propositions (known at PreToolUse):
  allowed_tool            the tool is one of this agent's registered tools
  probe_non_allowlisted   http_probe with a target outside the allowlist
  notify_info             notify with severity info
  notify_alert            notify with severity warning or critical
  restart                 restart_service                      (phase 2)
  cleanup                 cleanup_tmp                          (phase 2)

Result propositions (known at PostToolUse):
  status_checked          service_status returned a result
  logs_read               read_logs returned a result
  probe_ok                http_probe root_get returned HTTP 200
  notified                notify was accepted by at least one relay
  backup_ok               backup_db finished successfully      (phase 2)
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ltlguard.core.labeling import Candidate

SERVER_NAME = "blossoml"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
PROBE_TARGETS = ("root_get", "upload_head", "test_blob_head", "test_blob_get")

ALL_PROPS = frozenset(
    {
        "allowed_tool", "probe_non_allowlisted", "notify_info", "notify_alert", "restart", "cleanup",
        "status_checked", "logs_read", "probe_ok", "notified", "backup_ok",
    }
)


def full_name(short: str) -> str:
    return TOOL_PREFIX + short


class OpsLabeler:
    def __init__(self, tool_names: Iterable[str], representative_args: Mapping[str, list[Mapping[str, Any]]] | None = None):
        """``tool_names``: short names of the registered tools."""
        self.short_names = frozenset(tool_names)
        self.full_names = frozenset(full_name(t) for t in self.short_names)
        self.representative_args = dict(representative_args or {})

    def _short(self, tool: str) -> str | None:
        if tool in self.full_names:
            return tool[len(TOOL_PREFIX):]
        return None

    def call_props(self, tool: str, args: Mapping[str, Any]) -> frozenset[str]:
        short = self._short(tool)
        if short is None:
            return frozenset()
        props = {"allowed_tool"}
        if short == "http_probe":
            if set(args) != {"target"} or args.get("target") not in PROBE_TARGETS:
                props.add("probe_non_allowlisted")
        elif short == "notify":
            sev = args.get("severity")
            if sev == "info":
                props.add("notify_info")
            elif sev in ("warning", "critical"):
                props.add("notify_alert")
        elif short == "restart_service":
            props.add("restart")
        elif short == "cleanup_tmp":
            props.add("cleanup")
        return frozenset(props)

    def possible_result_props(self, tool: str, args: Mapping[str, Any]) -> frozenset[str]:
        short = self._short(tool)
        return frozenset(
            {
                "service_status": {"status_checked"},
                "read_logs": {"logs_read"},
                "notify": {"notified"},
                "backup_db": {"backup_ok"},
                "http_probe": {"probe_ok"} if args.get("target") == "root_get" else set(),
            }.get(short or "", set())
        )

    def result_props(self, tool: str, args: Mapping[str, Any], result: Mapping[str, Any] | None) -> frozenset[str]:
        short = self._short(tool)
        if short is None or result is None or "error" in result:
            return frozenset()
        out = set()
        if short == "service_status":
            out.add("status_checked")
        elif short == "read_logs":
            out.add("logs_read")
        elif short == "http_probe":
            if args.get("target") == "root_get" and result.get("status") == 200:
                out.add("probe_ok")
        elif short == "notify":
            accepted = result.get("accepted_relays")
            if isinstance(accepted, int) and accepted >= 1:
                out.add("notified")
        elif short == "backup_db":
            if result.get("ok") is True:
                out.add("backup_ok")
        return frozenset(out)

    def all_props(self) -> frozenset[str]:
        return ALL_PROPS

    def candidates(self) -> list[Candidate]:
        out = []
        for short in sorted(self.short_names):
            variants = self.representative_args.get(short) or [{}]
            for args in variants:
                label = short if len(variants) == 1 else f"{short}({', '.join(f'{k}={v}' for k, v in args.items())})"
                out.append(Candidate(label, full_name(short), args))
        return out
