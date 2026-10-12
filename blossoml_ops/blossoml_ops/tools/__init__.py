"""Tool registry for blossoml-ops."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from blossoml_ops.labeling import SERVER_NAME, full_name
from blossoml_ops.tools import db, logs, probe, storage, system, write_ops
from blossoml_ops.tools.base import Param, ToolError, ToolRunner, ToolSpec
from blossoml_ops.tools.executor import Executor, LocalExecutor


class MetricsHistory:
    """Values from the previous run (for day-over-day comparison). Saved at the end of a run."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self._previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._previous = {}
        self._current: dict[str, Any] = {}

    def previous(self, key: str) -> Any:
        return self._previous.get(key)

    def record(self, key: str, value: Any) -> None:
        self._current[key] = value

    def save(self, run_ts: str) -> None:
        if not self._current:
            return
        merged = {**self._previous, **self._current}
        merged["_saved_at"] = run_ts
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass
class ToolContext:
    config: Any
    session: Any                     # ltlguard GuardSession
    trace: Any                       # ltlguard TraceWriter
    notifier: Any                    # blossoml_ops.notify.notifier.Notifier
    history: MetricsHistory
    alert: Callable[[str, str, str], None]
    executor: Executor = field(default_factory=LocalExecutor)
    notify_results: list = field(default_factory=list)


async def notify(ctx: ToolContext, severity: str, title: str, body: str) -> dict:
    from blossoml_ops.notify.notifier import NotifyLimitExceeded

    try:
        outcome = await ctx.notifier.send(severity, title, body)
    except NotifyLimitExceeded:
        raise ToolError("このセッションで送れる通知の上限に達しています") from None
    ctx.notify_results.append((severity, outcome.ok))
    return {
        "accepted_relays": len(outcome.accepted),
        "accepted": outcome.accepted,
        "failed": outcome.failed,
    }


READ_ONLY_TOOLS = [
    ToolSpec(
        name="service_status",
        description="blossoML の systemd ユニットの状態（ActiveState、起動時刻、再起動回数、メモリ使用量）を返す。",
        params={},
        run=system.service_status,
        timeout_s=20,
    ),
    ToolSpec(
        name="read_logs",
        description=(
            "blossoML の journald ログを集計して返す。直近 since_hours 時間と、その直前の同じ長さの期間の件数を並べて返す。"
            "errors / uploads / deletes / mirror は該当するエラー行のサンプル（短く切り詰めたもの）も返す。"
            "直近のデプロイ（main.exe の更新）が期間内にあれば、デプロイ前後の内訳も返す。"
        ),
        params={
            "since_hours": Param("int", "集計する時間幅（時間）", minimum=1, maximum=72),
            "kind": Param("enum", "集計の種類", choices=logs.KINDS),
        },
        run=system.read_logs,
        timeout_s=150,
        degrade=logs.degrade,
        representative_args=[{"since_hours": 24, "kind": "all_summary"}],
    ),
    ToolSpec(
        name="http_probe",
        description=(
            "ローカルの blossoML に HTTP リクエストを送り、ステータス・所要時間・主要ヘッダを返す。"
            "root_get: GET /（利用規約テキストの先頭のみ返す）。upload_head: HEAD /upload（認証なしなので 401 が正常）。"
            "test_blob_head / test_blob_get: 運営者が用意したテスト用 blob のみ（GET は本文のハッシュ一致の真偽だけを返す）。"
        ),
        params={"target": Param("enum", "問い合わせ先", choices=probe.TARGETS)},
        run=probe.http_probe,
        timeout_s=20,
        representative_args=[{"target": "root_get"}],
    ),
    ToolSpec(
        name="disk_usage",
        description="ファイルシステムの空き容量と、data/ と db/ の使用量を返す（前回実行時の値があれば併記）。",
        params={},
        run=storage.disk_usage,
        timeout_s=60,
    ),
    ToolSpec(
        name="storage_audit",
        description=(
            "data/ ディレクトリを一覧して、blob ファイル数、tmp_* の残骸（名前・経過時間・サイズ）、命名規則に合わないファイル数、"
            "パーミッションの異常（他者が読めるファイル数など）を返す。ファイルの中身は読まない。"
        ),
        params={},
        run=storage.storage_audit,
        timeout_s=60,
    ),
    ToolSpec(
        name="db_report",
        description=(
            "blossom.db を読み取り専用で開き、名前付きの固定クエリの集計を返す。"
            "blob_counts_by_status / recent_uploads_count / owner_counts / consistency（DB とファイルの食い違い）。"
        ),
        params={"name": Param("enum", "レポート名", choices=db.REPORTS)},
        run=db.db_report,
        timeout_s=60,
        representative_args=[{"name": "blob_counts_by_status"}],
    ),
    ToolSpec(
        name="deploy_info",
        description="main.exe と main.exe.backup の更新時刻・サイズ・SHA-256 を返す。",
        params={},
        run=storage.deploy_info,
        timeout_s=60,
    ),
    ToolSpec(
        name="notify",
        description=(
            "運営者に Nostr の暗号化 DM で報告を送る。宛先は固定。info はセッションあたり 1 通、warning / critical は合計 5 通まで。"
            "結果として受理したリレーの数を返す（1 以上なら送信成功）。"
        ),
        params={
            "severity": Param("enum", "重大度", choices=("info", "warning", "critical")),
            "title": Param("str", "件名（100 文字以内）", max_len=100),
            "body": Param("str", "本文（2000 文字以内）", max_len=2000),
        },
        run=notify,
        timeout_s=120,
        representative_args=[
            # Only the fields that affect propositions; used for the "allowed operations" list.
            {"severity": "info"},
            {"severity": "warning"},
        ],
    ),
]

WRITE_TOOLS = [
    ToolSpec(
        name="backup_db",
        description="SQLite のオンラインバックアップで DB の日時付きバックアップを作る（root 所有スクリプト）。",
        params={},
        run=write_ops.backup_db,
        timeout_s=320,
        read_only=False,
    ),
    ToolSpec(
        name="restart_service",
        description="blossoML を再起動する。事前に service_status と read_logs で状況を確認すること。再起動後は http_probe(root_get) で稼働を確認すること。1 セッション 1 回まで。",
        params={},
        run=write_ops.restart_service,
        timeout_s=130,
        read_only=False,
    ),
    ToolSpec(
        name="cleanup_tmp",
        description="data/ の tmp_* のうち、指定時間より古いものを削除する。事前に backup_db が成功している必要がある。",
        params={"min_age_hours": Param("int", "この時間より古い tmp_* を削除する", minimum=6, maximum=168)},
        run=write_ops.cleanup_tmp,
        timeout_s=130,
        representative_args=[{"min_age_hours": 24}],
        read_only=False,
    ),
]


def build_specs(phase2_enabled: bool) -> list[ToolSpec]:
    return READ_ONLY_TOOLS + (WRITE_TOOLS if phase2_enabled else [])


def representative_args(specs: list[ToolSpec]) -> dict[str, list[dict]]:
    return {s.name: s.representative_args for s in specs if s.representative_args}


def build_sdk_server(specs: list[ToolSpec], ctx: ToolContext):
    """The in-process MCP server with our tools (claude_agent_sdk)."""
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from claude_agent_sdk import ToolAnnotations

    runner = ToolRunner(ctx)
    sdk_tools = []
    for spec in specs:
        async def handler(args, _spec=spec):
            return await runner.invoke(_spec, args)

        annotations = ToolAnnotations(readOnlyHint=spec.read_only, destructiveHint=not spec.read_only, openWorldHint=False)
        sdk_tools.append(tool(spec.name, spec.description, spec.json_schema(), annotations=annotations)(handler))
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=sdk_tools)


def allowed_tool_names(specs: list[ToolSpec]) -> list[str]:
    return [full_name(s.name) for s in specs]


__all__ = [
    "MetricsHistory", "ToolContext", "ToolSpec", "allowed_tool_names", "build_sdk_server", "build_specs", "representative_args",
]
