"""service_status and read_logs."""

from __future__ import annotations

import os
import time

from blossoml_ops.tools import logs
from blossoml_ops.tools.base import ToolError

_SHOW_PROPS = ("ActiveState", "SubState", "ActiveEnterTimestamp", "NRestarts", "MainPID", "MemoryCurrent", "Result")


def service_status(ctx) -> dict:
    unit = ctx.config.blossoml_unit
    r = ctx.executor.run(["/usr/bin/systemctl", "show", unit, "-p", ",".join(_SHOW_PROPS)], timeout=15)
    if r.returncode != 0:
        raise ToolError(f"systemctl show が終了コード {r.returncode} で失敗しました")
    values = {}
    for line in r.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep and k in _SHOW_PROPS:
            values[k] = v.strip()[:100]

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "unit": unit,
        "active_state": values.get("ActiveState"),
        "sub_state": values.get("SubState"),
        "result": values.get("Result"),
        "active_since": values.get("ActiveEnterTimestamp") or None,
        "n_restarts": as_int(values.get("NRestarts")),
        "main_pid": as_int(values.get("MainPID")),
        "memory_current_bytes": as_int(values.get("MemoryCurrent")),
    }


def read_logs(ctx, since_hours: int, kind: str) -> dict:
    now = time.time()
    since = now - 2 * since_hours * 3600  # current window + previous window for comparison
    try:
        deploy_time = os.stat(ctx.config.blossoml_dir / "main.exe").st_mtime
    except OSError:
        deploy_time = None
    entries = logs.journal_entries(ctx.config.blossoml_unit, since, ctx.executor)
    return logs.summarize(entries, kind, since_hours, now, deploy_time=deploy_time, mask_pubkeys=ctx.config.mask_pubkeys)
