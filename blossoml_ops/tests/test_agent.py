"""End-to-end harness test without the API.

A fake client plays the role of the Claude Code CLI: it calls the real hooks
(PreToolUse / PostToolUse / Stop) and the real tools through the in-process
MCP server, following a script of tool calls.
"""

import json

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

from ltlguard.trace.jsonl import read_events

from blossoml_ops.agent import (
    BUILTIN_TOOLS,
    EXIT_HARNESS_ALERT,
    EXIT_NOT_DELIVERED,
    EXIT_OK,
    EXIT_RULES_UNMET,
    build_options,
    check_options,
    run_agent,
)
from blossoml_ops.notify.keys import generate_secret
from blossoml_ops.notify.relay import PublishResult


class FakeClient:
    """Executes ``script`` (list of (tool, args)) and, when the Stop hook sends
    it back, ``on_reprompt`` (another list)."""

    def __init__(self, options, script, on_reprompt=()):
        self.options = options
        self.script = list(script)
        self.on_reprompt = list(on_reprompt)
        self.denied = []
        self.results = []
        self.stop_reasons = []
        self.queries = []
        self._pending = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _hook(self, event, payload):
        out = {}
        for matcher in self.options.hooks[event]:
            for h in matcher.hooks:
                out = await h(payload, payload.get("tool_use_id"), {"signal": None})
        return out

    async def _call(self, i, tool, args):
        base = {"session_id": "s", "transcript_path": "", "cwd": "", "tool_name": tool, "tool_input": args, "tool_use_id": f"t{i}"}
        pre = await self._hook("PreToolUse", {**base, "hook_event_name": "PreToolUse"})
        decision = pre.get("hookSpecificOutput", {}).get("permissionDecision")
        if decision == "deny":
            self.denied.append((tool, pre["hookSpecificOutput"]["permissionDecisionReason"]))
            return
        if not tool.startswith("mcp__blossoml__"):
            self.denied.append((tool, "unknown tool"))
            return
        tools = {t.name: t for t in self.options.mcp_servers["blossoml"]["tools"]}
        res = await tools[tool[len("mcp__blossoml__"):]].handler(args)
        text = res["content"][0]["text"]
        self.results.append((tool, json.loads(text)))
        await self._hook("PostToolUse", {**base, "hook_event_name": "PostToolUse", "tool_response": text})

    async def query(self, prompt):
        self.queries.append(prompt)
        self._pending = self.script if len(self.queries) == 1 else self.on_reprompt
        if len(self.queries) > 1:
            self.on_reprompt = []

    async def receive_response(self):
        steps, self._pending = self._pending, []
        for i, (tool, args) in enumerate(steps):
            yield AssistantMessage(content=[ToolUseBlock(id=f"t{i}", name=tool, input=args)], model="fake")
            await self._call(len(self.results) + len(self.denied) + i, tool, args)
        while True:
            stop = await self._hook("Stop", {"session_id": "s", "transcript_path": "", "cwd": "", "hook_event_name": "Stop", "stop_hook_active": False})
            if stop.get("decision") != "block":
                break
            self.stop_reasons.append(stop["reason"])
            steps, self.on_reprompt = self.on_reprompt, []
            if not steps:
                continue
            for i, (tool, args) in enumerate(steps):
                await self._call(100 + i, tool, args)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=3, session_id="s", total_cost_usd=0.01)


def make_publisher(accept=True):
    sent = []

    async def publisher(relays, event, **kw):
        sent.append(event)
        return [PublishResult(r, accept, "") for r in relays]

    publisher.sent = sent
    return publisher


@pytest.fixture(autouse=True)
def capture_sdk_tools(monkeypatch):
    """Keep the SdkMcpTool objects so the fake client can call their handlers."""
    import claude_agent_sdk

    monkeypatch.setattr(
        claude_agent_sdk, "create_sdk_mcp_server",
        lambda name, version="1.0.0", tools=None: {"type": "sdk", "name": name, "instance": None, "tools": tools},
    )


async def run(config, script, on_reprompt=(), accept=True, **cfg):
    holder = {}

    def factory(options):
        holder["client"] = FakeClient(options, script, on_reprompt)
        return holder["client"]

    pub = make_publisher(accept)
    report = await run_agent(config(**cfg), "daily", api_key="sk-test", nostr_secret=generate_secret(), client_factory=factory, publisher=pub)
    return report, holder["client"], pub


def trace_events(cfg_dir):
    return list(read_events(sorted((cfg_dir / "state" / "traces").glob("*.jsonl"))))


INFO = ("mcp__blossoml__notify", {"severity": "info", "title": "異常なし", "body": "点検の結果、異常はありません。"})


async def test_daily_happy_path(config, tmp_path):
    script = [
        ("mcp__blossoml__storage_audit", {}),
        ("mcp__blossoml__db_report", {"name": "consistency"}),
        INFO,
    ]
    report, client, pub = await run(config, script)
    assert report.exit_code == EXIT_OK, report
    assert not client.denied and not client.stop_reasons
    assert len(pub.sent) == 1 and pub.sent[0]["kind"] == 1059
    kinds = [e["event"] for e in trace_events(tmp_path)]
    assert kinds[:2] == ["start", "rules"] and kinds.count("pre") == 3 and kinds.count("post") == 3 and kinds[-1] == "end"
    assert "sk-test" not in json.dumps(trace_events(tmp_path))


async def test_builtin_tool_attempt_is_denied_and_reported(config, tmp_path):
    script = [("Bash", {"command": "cat /home/ubuntu/blossoML/data/*"}), ("mcp__blossoml__http_probe", {"target": "root_get", "x": 1}), INFO]
    report, client, pub = await run(config, script)
    assert [d[0] for d in client.denied] == ["Bash", "mcp__blossoml__http_probe"]
    assert "R0" in client.denied[0][1] and "現在実行できる操作" in client.denied[0][1]
    assert "R6" in client.denied[1][1]
    ev = trace_events(tmp_path)
    denies = [e for e in ev if e["event"] == "deny"]
    assert {d["tool"] for d in denies} >= {"Bash", "mcp__blossoml__http_probe"}
    assert report.exit_code == EXIT_HARNESS_ALERT
    assert len(pub.sent) == 2  # Claude's info + one aggregated harness alert


async def test_stop_hook_sends_agent_back_to_report(config, tmp_path):
    report, client, pub = await run(config, [("mcp__blossoml__storage_audit", {})], on_reprompt=[INFO])
    assert len(client.stop_reasons) == 1 and "R5'" in client.stop_reasons[0]
    assert report.exit_code == EXIT_OK


async def test_rules_unmet_after_reprompts(config, tmp_path):
    report, client, pub = await run(config, [("mcp__blossoml__storage_audit", {})])
    assert len(client.stop_reasons) == 2
    assert report.exit_code == EXIT_NOT_DELIVERED  # no report at all
    assert len(pub.sent) == 1  # the harness alert


@pytest.fixture
def fake_host(monkeypatch):
    """systemctl, journalctl and sudo as seen on a healthy host."""
    from blossoml_ops.tools.executor import CommandResult, LocalExecutor

    calls = []

    def run(self, argv, timeout=30.0):
        calls.append(list(argv))
        if argv[:2] == ["/usr/bin/systemctl", "show"]:
            return CommandResult(0, "ActiveState=active\nSubState=running\nNRestarts=0\n", "")
        return CommandResult(0, "", "")

    monkeypatch.setattr(LocalExecutor, "run", run)
    monkeypatch.setattr(LocalExecutor, "stream_lines", lambda self, argv, timeout=0, max_lines=0: iter(()))
    return calls


async def test_rules_unmet_with_report_delivered(config, tmp_path, fake_host):
    script = [("mcp__blossoml__service_status", {}), ("mcp__blossoml__read_logs", {"since_hours": 1, "kind": "errors"}),
              ("mcp__blossoml__restart_service", {}), INFO]
    report, client, pub = await run(config, script, phase2={"enabled": True})
    assert not client.denied
    assert len(client.stop_reasons) == 2 and "R4" in client.stop_reasons[0]  # no probe after restart
    assert report.exit_code == EXIT_RULES_UNMET
    assert ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "blossoml"] in fake_host


async def test_second_info_is_denied(config, tmp_path):
    report, client, pub = await run(config, [INFO, INFO])
    assert len(client.denied) == 1 and "R8" in client.denied[0][1]
    assert report.exit_code == EXIT_OK


async def test_nothing_delivered(config, tmp_path):
    report, client, pub = await run(config, [INFO], accept=False)
    assert report.exit_code == EXIT_NOT_DELIVERED
    assert (tmp_path / "state" / "undelivered.jsonl").exists()


async def test_phase2_restart_flow(config, tmp_path):
    script = [
        ("mcp__blossoml__restart_service", {}),
        INFO,
    ]
    report, client, pub = await run(config, script, phase2={"enabled": True})
    assert "R1" in client.denied[0][1]


def test_options_disable_everything_builtin(config):
    from blossoml_ops.agent import build_env

    cfg = config(proxy="http://127.0.0.1:8888")
    opts = build_options(cfg, system_prompt="x", allowed=["mcp__blossoml__notify"], server={"type": "sdk"}, hooks={
        k: [object()] for k in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")}, env=build_env(cfg, "k"))
    assert opts.tools == [] and opts.permission_mode == "dontAsk" and opts.setting_sources == []
    assert set(BUILTIN_TOOLS) <= set(opts.disallowed_tools)
    assert check_options(opts, ["mcp__blossoml__notify"]) == []
    assert opts.env["HTTPS_PROXY"] == "http://127.0.0.1:8888" and "localhost" in opts.env["NO_PROXY"]
    opts.permission_mode = "bypassPermissions"
    assert check_options(opts, ["mcp__blossoml__notify"])


async def test_traced_args_are_checked(config, tmp_path):
    import base64

    body = "x " + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30).decode()
    report, client, pub = await run(config, [("mcp__blossoml__notify", {"severity": "info", "title": "t", "body": body}), INFO])
    ev = trace_events(tmp_path)
    pre = [e for e in ev if e["event"] == "pre"]
    assert set(pre[0]["args"]) == {"_omitted"} and pre[0]["args"]["_omitted"].endswith("png")
    assert "iVBOR" not in json.dumps(ev)
    assert client.results[0][1]["error"] == "invalid_arguments"


async def test_broken_rules_refuse_and_alert(config, tmp_path, monkeypatch):
    import blossoml_ops.agent as agent_mod

    edited = tmp_path / "rules.yaml"
    edited.write_text(agent_mod.RULES_YAML.read_text().replace("template: eventually", "formula: 'F (notified'", 1))
    monkeypatch.setattr(agent_mod, "RULES_YAML", edited)
    pub = make_publisher()
    report = await run_agent(config(), "daily", api_key="k", nostr_secret=generate_secret(),
                             client_factory=lambda o: pytest.fail("must not start the agent"), publisher=pub)
    from blossoml_ops.agent import EXIT_CONFIG

    assert report.exit_code == EXIT_CONFIG
    assert len(pub.sent) == 1  # the critical harness DM
