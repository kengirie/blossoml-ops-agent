"""The agent run: SDK options, the guard wiring, and the message loop."""

from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ltlguard.core.automaton import RuleSet, RulesError, load_rules
from ltlguard.core.monitor import Monitor
from ltlguard.core.session import GuardSession
from ltlguard.trace.jsonl import TraceWriter

from blossoml_ops.config import Config
from blossoml_ops.guard import args_for_trace
from blossoml_ops.labeling import OpsLabeler, full_name
from blossoml_ops.tools import (
    MetricsHistory,
    ToolContext,
    allowed_tool_names,
    build_sdk_server,
    build_specs,
    representative_args,
)

PACKAGE_DIR = Path(__file__).parent
RULES_YAML = PACKAGE_DIR / "rules.yaml"
PROMPT_MD = PACKAGE_DIR / "prompt.md"

# Built-in Claude Code tools. `tools=[]` already removes them; listing them as
# disallowed as well keeps them out even if that option's behavior changes.
BUILTIN_TOOLS = (
    "Agent", "Bash", "BashOutput", "Edit", "ExitPlanMode", "Glob", "Grep", "KillBash", "KillShell",
    "ListMcpResourcesTool", "LSP", "MultiEdit", "NotebookEdit", "NotebookRead", "Read", "ReadMcpResourceTool",
    "Skill", "SlashCommand", "Task", "TaskOutput", "TaskStop", "TodoRead", "TodoWrite", "WebFetch", "WebSearch", "Write",
    "EnterPlanMode", "EnterWorktree", "ExitWorktree", "AskUserQuestion", "CronCreate", "CronDelete", "CronList",
    "Monitor", "PushNotification", "RemoteTrigger", "SendMessage", "ToolSearch", "Workflow",
)

# Extra CLI flags (Claude Code 2.1.x):
#   --restricted              refuses bypassPermissions, drops code-running tools, ignores settings files
#   --disable-slash-commands  no skills or slash commands
#   --no-chrome               no browser integration
#   --no-session-persistence  no transcripts on disk (they would contain tool output)
# (--safe-mode is not usable: it also disables hooks and MCP servers.)
EXTRA_CLI_FLAGS = {"restricted": None, "disable-slash-commands": None, "no-chrome": None, "no-session-persistence": None}

MODES = ("daily", "postdeploy", "manual")

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_RULES_UNMET = 2
EXIT_NOT_DELIVERED = 3
EXIT_HARNESS_ALERT = 4
EXIT_CONFIG = 5

PHASE2_PROMPT = """
# 障害の一次対応（書き込み系ツール）

- restart_service: サービスが停止している、または応答しない場合にのみ使います。事前に service_status と read_logs で状況を確認し、再起動後は http_probe（root_get）で 200 を確認してください。1 セッション 1 回までです。再起動しても直らない場合は、直前の版へのロールバックを「提案」として critical で報告してください（ロールバックは運営者が行います）。
- backup_db / cleanup_tmp: 24 時間以上前の tmp_* が残っている場合に、backup_db を成功させてから cleanup_tmp を使えます。
- 上記以外の目的で書き込み系ツールを使わないでください。
"""

MODE_PROMPTS = {
    "daily": """定期点検（daily）を行ってください。

1. service_status でサービスの状態を確認する。
2. http_probe で root_get と upload_head を確認する。
3. disk_usage と storage_audit で容量と data/ の状態を確認する。
4. read_logs で since_hours=24 の all_summary と errors を確認する。必要なら他の kind も見る。
5. db_report で blob_counts_by_status、recent_uploads_count、owner_counts、consistency を確認する。
6. deploy_info で最後のデプロイ時刻を確認する。
7. 判断基準に従って重大度を決め、notify で報告する（異常なしなら info 1 通）。
""",
    "postdeploy": """デプロイ後の確認（postdeploy）を行ってください。

1. deploy_info で main.exe の更新時刻とハッシュ、backup の有無を確認する。
2. service_status で、デプロイ後に起動していること（起動時刻が更新時刻より後）を確認する。
3. http_probe で root_get、upload_head、test_blob_head、test_blob_get を確認する（test_blob_get は sha256_match が true であること）。
4. read_logs で since_hours=6 程度の errors と status_codes を確認し、split_at_last_deploy があればデプロイ前後を比較する。
5. 結果を notify で報告する（問題なしなら info、問題があれば warning / critical）。
""",
    "manual": """運営者からの調査依頼（manual）です。読み取り系のツールで調べ、結果を notify で報告してください。

依頼内容（運営者が書いたものです）:
{instruction}
""",
}


def build_system_prompt(cfg: Config) -> str:
    text = PROMPT_MD.read_text(encoding="utf-8")
    if cfg.phase2_enabled:
        text += PHASE2_PROMPT
    return text


def build_user_prompt(mode: str, instruction: str | None) -> str:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if mode == "manual":
        if not instruction or not instruction.strip():
            raise ValueError("manual mode needs an instruction")
        return MODE_PROMPTS["manual"].format(instruction=instruction.strip()[:4000])
    return MODE_PROMPTS[mode]


def build_env(cfg: Config, api_key: str | None) -> dict[str, str]:
    env = {
        "CLAUDE_CONFIG_DIR": str(cfg.state_dir / "claude"),
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
    if api_key is not None:
        env["ANTHROPIC_API_KEY"] = api_key
    if cfg.proxy:
        env["HTTPS_PROXY"] = cfg.proxy
        env["HTTP_PROXY"] = cfg.proxy
    return env


def build_options(cfg: Config, *, system_prompt: str, allowed: list[str], server: Any, hooks: dict, env: dict):
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        tools=[],                         # no built-in tools at all
        allowed_tools=list(allowed),      # our tools run without a permission prompt
        disallowed_tools=list(BUILTIN_TOOLS),
        mcp_servers={"blossoml": server},
        strict_mcp_config=True,           # ignore any other MCP configuration
        permission_mode="dontAsk",        # anything not pre-approved is denied, never asked
        setting_sources=[],               # no CLAUDE.md, settings, output styles from disk
        skills=[],
        plugins=[],
        agents=None,
        cwd=str(cfg.work_dir),
        hooks=hooks,
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        model=cfg.model,
        env=env,
        extra_args=dict(EXTRA_CLI_FLAGS),
    )


def check_options(options: Any, allowed: list[str]) -> list[str]:
    """Return problems with the SDK options (used by selfcheck and before every run)."""
    problems = []
    if options.tools != []:
        problems.append("built-in tools are not disabled (tools must be [])")
    if options.permission_mode != "dontAsk":
        problems.append("permission_mode must be dontAsk")
    if options.setting_sources != []:
        problems.append("setting_sources must be []")
    if not options.strict_mcp_config:
        problems.append("strict_mcp_config must be true")
    if sorted(options.allowed_tools) != sorted(allowed):
        problems.append("allowed_tools must be exactly the registered tools")
    missing = set(BUILTIN_TOOLS) - set(options.disallowed_tools)
    if missing:
        problems.append(f"built-in tools not in disallowed_tools: {sorted(missing)}")
    if set(options.allowed_tools) & set(BUILTIN_TOOLS):
        problems.append("a built-in tool is in allowed_tools")
    if options.agents:
        problems.append("subagents must not be defined")
    if options.plugins:
        problems.append("plugins must not be loaded")
    if options.skills != []:
        problems.append("skills must be []")
    if list(options.mcp_servers) != ["blossoml"]:
        problems.append("only the in-process blossoml MCP server may be configured")
    for flag in EXTRA_CLI_FLAGS:
        if flag not in (options.extra_args or {}):
            problems.append(f"CLI flag --{flag} is not set")
    for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"):
        if not (options.hooks or {}).get(name):
            problems.append(f"hook {name} is not installed")
    return problems


def check_rules(ruleset: RuleSet, labeler: OpsLabeler) -> list[str]:
    problems = []
    unknown = ruleset.aps - labeler.all_props()
    if unknown:
        problems.append(f"rules use propositions the labeler never produces: {sorted(unknown)}")
    for rid in ("R0", "R6", "R5'", "R8"):
        try:
            r = ruleset.by_id(rid)
        except KeyError:
            problems.append(f"required rule {rid} is missing")
            continue
        if not r.enforced:
            problems.append(f"rule {rid} must be enforced (on_violation: deny)")
    return problems


@dataclass
class Alerts:
    items: list[tuple[str, str, str]] = field(default_factory=list)

    def __call__(self, severity: str, title: str, body: str) -> None:
        if (severity, title, body) not in self.items:
            self.items.append((severity, title, body))

    def summary(self) -> tuple[str, str, str] | None:
        if not self.items:
            return None
        order = {"info": 0, "warning": 1, "critical": 2}
        severity = max((i[0] for i in self.items), key=lambda s: order.get(s, 2))
        body = "\n".join(f"- [{s}] {t}: {b}" for s, t, b in self.items)
        return severity, "エージェント実行時の異常（ハーネスより）", body[:3000]


@dataclass
class RunReport:
    run_id: str
    exit_code: int = EXIT_OK
    result_subtype: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    sdk_session_id: str | None = None
    notes: list[str] = field(default_factory=list)


async def _drive(client, prompt: str, session: GuardSession, trace: TraceWriter, alerts: Alerts, allowed: set[str], report: RunReport) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    await client.query(prompt)
    while True:
        result = None
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock) and block.name not in allowed:
                        # The CLI may reject unknown tools before hooks run; record it here too.
                        trace.write("deny", tool=block.name, call_id=block.id, source="harness", reason="not_allowlisted")
                        alerts("critical", "許可リスト外のツール呼び出し", f"Claude が {block.name[:80]} を呼ぼうとしました（拒否済み）。")
            elif isinstance(msg, ResultMessage):
                result = msg
        if result is None:
            report.notes.append("no result message")
            return
        report.result_subtype = result.subtype
        report.cost_usd = result.total_cost_usd
        report.num_turns = result.num_turns
        report.sdk_session_id = result.session_id
        trace.write(
            "result", subtype=result.subtype, is_error=result.is_error, num_turns=result.num_turns,
            cost_usd=result.total_cost_usd, permission_denials=len(result.permission_denials or []),
        )
        if result.subtype in ("error_max_turns", "error_max_budget_usd"):
            alerts("critical", "実行上限に達しました", f"{result.subtype}（ターン数 {result.num_turns}、費用 {result.total_cost_usd} USD）")
            return
        if result.is_error:
            alerts("critical", "エージェントの実行エラー", f"subtype={result.subtype}")
            return
        # Fallback in case the Stop hook did not send the agent back.
        if not session.monitor.pending_obligations() or session.unmet_at_stop:
            return
        message = session.at_stop()
        if message is None:
            return
        await client.query(message)


async def run_agent(
    cfg: Config,
    mode: str,
    instruction: str | None = None,
    *,
    api_key: str,
    nostr_secret: bytes,
    client_factory=None,
    publisher=None,
) -> RunReport:
    """One agent run. ``client_factory`` and ``publisher`` exist for tests."""
    from claude_agent_sdk import ClaudeSDKClient

    from ltlguard.adapters.claude_agent_sdk import ClaudeAgentSdkAdapter

    from blossoml_ops.notify.notifier import Notifier

    client_factory = client_factory or (lambda options: ClaudeSDKClient(options=options))

    run_id = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    report = RunReport(run_id=run_id)
    for d in (cfg.state_dir, cfg.trace_dir, cfg.work_dir, cfg.state_dir / "claude"):
        d.mkdir(parents=True, exist_ok=True)

    trace = TraceWriter(cfg.trace_dir, run_id, retention_days=cfg.trace_retention_days, max_total_bytes=cfg.trace_max_total_mb * 1024 * 1024)
    trace.write("start", mode=mode)
    alerts = Alerts()
    notifier_kw = {"publisher": publisher} if publisher else {}
    notifier = Notifier(nostr_secret, run_id, proxy=cfg.proxy, undelivered_path=cfg.undelivered_path, **notifier_kw)

    specs = build_specs(cfg.phase2_enabled)
    allowed = allowed_tool_names(specs)
    labeler = OpsLabeler([s.name for s in specs], representative_args(specs))
    try:
        ruleset = load_rules(RULES_YAML)
    except RulesError as e:
        # Refuse to run with broken rules, but still tell the operator.
        trace.write("refused", problems=["rules"])
        alerts("critical", "ルールを読み込めないため実行しませんでした", str(e)[:500])
        report.exit_code = EXIT_CONFIG
        await _send_harness_alerts(alerts, notifier, trace)
        trace.write("end", exit_code=report.exit_code)
        return report
    trace.write("rules", source_sha256=ruleset.source_sha256, rules=[r.id for r in ruleset.rules], phase2=cfg.phase2_enabled)
    problems = check_rules(ruleset, labeler)
    session = GuardSession(
        Monitor(ruleset), labeler, trace, on_alert=alerts, state_path=str(cfg.state_dir / "monitor-state.json"),
        args_filter=args_for_trace,
    )
    adapter = ClaudeAgentSdkAdapter(session)
    history = MetricsHistory(cfg.state_dir / "metrics.json")
    ctx = ToolContext(config=cfg, session=session, trace=trace, notifier=notifier, history=history, alert=alerts)
    server = build_sdk_server(specs, ctx)
    options = build_options(
        cfg, system_prompt=build_system_prompt(cfg), allowed=allowed, server=server,
        hooks=adapter.hooks(), env=build_env(cfg, api_key),
    )
    problems += check_options(options, allowed)
    if problems:
        trace.write("refused", problems=problems)
        alerts("critical", "起動前チェックに失敗しました", "; ".join(problems))
        report.exit_code = EXIT_CONFIG
        await _finish(cfg, report, alerts, notifier, trace, history, session)
        return report

    prompt = build_user_prompt(mode, instruction)
    try:
        async with client_factory(options) as client:
            await asyncio.wait_for(_drive(client, prompt, session, trace, alerts, set(allowed), report), cfg.run_timeout_s)
    except asyncio.TimeoutError:
        alerts("critical", "実行時間の上限に達しました", f"{cfg.run_timeout_s} 秒で打ち切りました。")
    except Exception as e:
        trace.write("error", error=type(e).__name__)
        alerts("critical", "エージェントの実行に失敗しました", f"例外: {type(e).__name__}")
        report.exit_code = EXIT_INTERNAL

    await _finish(cfg, report, alerts, notifier, trace, history, session)
    return report


async def _send_harness_alerts(alerts: Alerts, notifier, trace: TraceWriter) -> None:
    summary = alerts.summary()
    if not summary:
        return
    severity, title, body = summary
    try:
        outcome = await notifier.send(severity, title, body, harness=True)
        trace.write("harness_alert", delivered=outcome.ok, count=len(alerts.items))
    except Exception as e:
        trace.write("harness_alert", delivered=False, error=type(e).__name__)


async def _finish(cfg, report: RunReport, alerts: Alerts, notifier, trace: TraceWriter, history: MetricsHistory, session: GuardSession) -> None:
    if session.unmet_at_stop:
        alerts("critical", "ルール未達で終了しました", f"未履行のルール: {', '.join(session.unmet_at_stop)}")
    elif session.monitor.pending_obligations() and report.exit_code == EXIT_OK:
        pending = [r.id for r in session.monitor.pending_obligations()]
        session.unmet_at_stop = pending
        alerts("critical", "ルール未達で終了しました", f"未履行のルール: {', '.join(pending)}")
    if session.broken:
        alerts("critical", "LTL モニタが異常状態で終了しました", session.broken)
    claude_delivered = notifier.any_delivered  # before the harness sends anything itself
    if not claude_delivered and report.exit_code == EXIT_OK:
        alerts("critical", "報告が運営者に届いていません", "このセッションで受理された通知がありません。")

    try:
        history.save(report.run_id)
    except OSError:
        report.notes.append("could not save metrics history")

    await _send_harness_alerts(alerts, notifier, trace)

    if report.exit_code == EXIT_OK:
        # No delivered report is the root cause when R5' is also unmet, so it wins.
        if not claude_delivered:
            report.exit_code = EXIT_NOT_DELIVERED
        elif session.unmet_at_stop:
            report.exit_code = EXIT_RULES_UNMET
        elif alerts.items:
            report.exit_code = EXIT_HARNESS_ALERT
    trace.write("end", exit_code=report.exit_code, cost_usd=report.cost_usd, turns=report.num_turns, monitor=session.monitor.describe())


__all__ = ["MODES", "run_agent", "build_options", "check_options", "check_rules", "full_name"]
