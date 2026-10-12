"""Sending operator notifications as NIP-17 DMs.

The harness formats the message; Claude only supplies severity, title and body.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from blossoml_ops.notify import RECIPIENT_HEX, RELAYS
from blossoml_ops.notify.nip17 import wrap_dm
from blossoml_ops.notify.relay import PublishResult, publish

SEVERITIES = ("info", "warning", "critical")
MAX_INFO_PER_RUN = 1
MAX_ALERTS_PER_RUN = 5

Publisher = Callable[..., Awaitable[list[PublishResult]]]


class NotifyLimitExceeded(Exception):
    pass


def format_message(severity: str, title: str, body: str, run_id: str, now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return f"[{severity}] blossoML: {title}\n\n{body}\n\n(run: {run_id}, {now.isoformat(timespec='seconds')})"


@dataclass
class SendOutcome:
    accepted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.accepted)


class Notifier:
    def __init__(
        self,
        secret: bytes,
        run_id: str,
        *,
        proxy: str | None,
        undelivered_path: Path,
        publisher: Publisher = publish,
        recipient_hex: str = RECIPIENT_HEX,
        relays: tuple[str, ...] = RELAYS,
        total_timeout: float = 90.0,
    ):
        self._secret = secret
        self.run_id = run_id
        self.proxy = proxy
        self.undelivered_path = undelivered_path
        self.publisher = publisher
        self.recipient_hex = recipient_hex
        self.relays = relays
        self.total_timeout = total_timeout
        self.sent_info = 0
        self.sent_alerts = 0
        self.any_delivered = False
        self.any_failed = False

    async def send(self, severity: str, title: str, body: str, *, harness: bool = False) -> SendOutcome:
        """Send one DM. ``harness=True`` is for messages from the harness itself
        (guard failures etc.); those do not count against Claude's limits."""
        if severity not in SEVERITIES:
            raise ValueError("invalid severity")
        if not harness:
            # Defense in depth; the LTL rules R8/R9 enforce the same limits.
            if severity == "info" and self.sent_info >= MAX_INFO_PER_RUN:
                raise NotifyLimitExceeded("info")
            if severity != "info" and self.sent_alerts >= MAX_ALERTS_PER_RUN:
                raise NotifyLimitExceeded("alert")
            if severity == "info":
                self.sent_info += 1
            else:
                self.sent_alerts += 1
        text = format_message(severity, title, body, self.run_id)
        wrap = wrap_dm(self._secret, self.recipient_hex, text)
        results = await self.publisher(
            self.relays, wrap, auth_secret=self._secret, proxy=self.proxy, total_timeout=self.total_timeout
        )
        outcome = SendOutcome(
            accepted=[r.relay for r in results if r.accepted],
            failed=[r.relay for r in results if not r.accepted],
        )
        if outcome.ok:
            self.any_delivered = True
        else:
            self.any_failed = True
            self._record_undelivered(severity, title, body, results)
        return outcome

    def _record_undelivered(self, severity: str, title: str, body: str, results: list[PublishResult]) -> None:
        rec = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "run": self.run_id,
            "severity": severity,
            "title": title,
            "body": body,
            "relays": {r.relay: r.message for r in results},
        }
        self.undelivered_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.undelivered_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
