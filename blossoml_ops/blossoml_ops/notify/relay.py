"""Publishing events to Nostr relays (NIP-01 ``EVENT``/``OK``, NIP-42 ``AUTH``)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from blossoml_ops.notify.keys import finalize_event
from blossoml_ops.tls import client_context


@dataclass
class PublishResult:
    relay: str
    accepted: bool
    message: str  # short, relay-provided reason (sanitized by the caller before use)


async def publish_once(
    relay: str,
    event: dict[str, Any],
    *,
    auth_secret: bytes | None,
    proxy: str | None,
    timeout: float = 15.0,
) -> PublishResult:
    from websockets.asyncio.client import connect

    deadline = time.monotonic() + timeout
    authed = False

    async def recv(ws):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return json.loads(await asyncio.wait_for(ws.recv(), remaining))

    tls = client_context() if relay.startswith("wss://") else None
    async with connect(relay, proxy=proxy, ssl=tls, open_timeout=timeout, close_timeout=2, max_size=1 << 20) as ws:
        await ws.send(json.dumps(["EVENT", event]))
        while True:
            msg = await recv(ws)
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == "AUTH" and len(msg) >= 2 and auth_secret is not None and not authed:
                # NIP-42: authenticate as the bot, then resend.
                auth = finalize_event(
                    {
                        "created_at": int(time.time()),
                        "kind": 22242,
                        "tags": [["relay", relay], ["challenge", str(msg[1])]],
                        "content": "",
                    },
                    auth_secret,
                )
                await ws.send(json.dumps(["AUTH", auth]))
                authed = True
                continue
            # Only the OK for our event counts (the OK for an AUTH event is ignored).
            if msg[0] == "OK" and len(msg) >= 3 and msg[1] == event["id"]:
                accepted = bool(msg[2])
                reason = str(msg[3]) if len(msg) > 3 else ""
                if not accepted and reason.startswith("auth-required") and authed:
                    await ws.send(json.dumps(["EVENT", event]))
                    continue
                return PublishResult(relay, accepted, reason[:120])


async def publish(
    relays: tuple[str, ...] | list[str],
    event: dict[str, Any],
    *,
    auth_secret: bytes | None,
    proxy: str | None,
    attempt_timeout: float = 15.0,
    total_timeout: float = 90.0,
) -> list[PublishResult]:
    """Publish to all relays in parallel. Relays that fail are retried with
    exponential backoff until ``total_timeout``."""
    deadline = time.monotonic() + total_timeout

    async def one(relay: str) -> PublishResult:
        delay = 2.0
        last = PublishResult(relay, False, "not attempted")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                return last
            try:
                last = await publish_once(relay, event, auth_secret=auth_secret, proxy=proxy, timeout=min(attempt_timeout, remaining))
            except Exception as e:
                last = PublishResult(relay, False, type(e).__name__)
            if last.accepted:
                return last
            if last.message.startswith(("blocked", "invalid", "pow")):
                return last  # retrying will not help
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay *= 2

    return list(await asyncio.gather(*(one(r) for r in relays)))
