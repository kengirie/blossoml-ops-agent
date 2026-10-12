"""Phase 2 write operations.

The agent only asks systemd (through sudo with a fixed argv) to start
root-owned oneshot units; the work runs there, outside the agent's sandbox.
Rollback of the server binary needs a human approval flow, which is out of
scope, so there is no rollback tool.
"""

from __future__ import annotations

import re
import time

CLEANUP_AGES = (6, 12, 24, 48, 72, 168)  # must match sudoers
_CTRL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _tail(text: str, lines: int = 5, width: int = 200) -> list[str]:
    return [_CTRL_RE.sub(" ", line)[:width] for line in text.strip().splitlines()[-lines:]]


def _start_unit(ctx, unit: str, timeout: float) -> dict:
    since = int(time.time()) - 1
    r = ctx.executor.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "--wait", unit], timeout=timeout)
    out = {"ok": r.returncode == 0, "exit_code": r.returncode}
    # The unit's own output (e.g. "backup ... integrity=ok"); the agent user can read the journal.
    log = ctx.executor.run(["/usr/bin/journalctl", "-u", unit, f"--since=@{since}", "-o", "cat", "--no-pager", "-q"], timeout=15)
    out["output"] = _tail(log.stdout)
    return out


def backup_db(ctx) -> dict:
    return _start_unit(ctx, "blossoml-ops-backup-db.service", timeout=320)


def restart_service(ctx) -> dict:
    r = ctx.executor.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "blossoml"], timeout=120)
    return {"ok": r.returncode == 0, "exit_code": r.returncode}


def cleanup_tmp(ctx, min_age_hours: int) -> dict:
    if min_age_hours not in CLEANUP_AGES:  # validated before; defense in depth
        raise ValueError("min_age_hours")
    return _start_unit(ctx, f"blossoml-ops-cleanup-tmp@{min_age_hours}.service", timeout=130)
