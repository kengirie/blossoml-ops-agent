"""Phase 2 write operations, via root-owned scripts allowed in sudoers with a
fixed argv. Rollback of the server binary needs a human approval flow, which
is out of scope, so there is no rollback tool.
"""

from __future__ import annotations

import re

LIBEXEC = "/usr/local/libexec/blossoml-ops"
_CTRL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _tail(text: str, lines: int = 5, width: int = 200) -> list[str]:
    out = []
    for line in text.strip().splitlines()[-lines:]:
        out.append(_CTRL_RE.sub(" ", line)[:width])
    return out


def _sudo(ctx, argv: list[str], timeout: float) -> dict:
    r = ctx.executor.run(["/usr/bin/sudo", "-n", *argv], timeout=timeout)
    return {"ok": r.returncode == 0, "exit_code": r.returncode, "output": _tail(r.stdout)}


def backup_db(ctx) -> dict:
    return _sudo(ctx, [f"{LIBEXEC}/backup-db"], timeout=300)


def restart_service(ctx) -> dict:
    return _sudo(ctx, ["/usr/bin/systemctl", "restart", "blossoml"], timeout=120)


def cleanup_tmp(ctx, min_age_hours: int) -> dict:
    return _sudo(ctx, [f"{LIBEXEC}/cleanup-tmp", str(int(min_age_hours))], timeout=120)
