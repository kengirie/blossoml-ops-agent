"""NIP-17 private direct messages: kind 14 rumor -> kind 13 seal -> kind 1059 gift wrap (NIP-59)."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from blossoml_ops.notify import nip44
from blossoml_ops.notify.keys import event_id, finalize_event, generate_secret, xonly_pubkey

TWO_DAYS = 2 * 24 * 3600


def _randomized_now(now: int) -> int:
    # NIP-59: tweak created_at into the past to hinder timing analysis.
    return now - secrets.randbelow(TWO_DAYS)


def make_rumor(sender_secret: bytes, recipient_hex: str, content: str, now: int | None = None) -> dict[str, Any]:
    ev = {
        "pubkey": xonly_pubkey(sender_secret),
        "created_at": int(now if now is not None else time.time()),
        "kind": 14,
        "tags": [["p", recipient_hex]],
        "content": content,
    }
    ev["id"] = event_id(ev)
    return ev  # unsigned on purpose


def make_seal(sender_secret: bytes, recipient_hex: str, rumor: dict[str, Any], now: int) -> dict[str, Any]:
    ck = nip44.get_conversation_key(sender_secret, recipient_hex)
    return finalize_event(
        {
            "created_at": _randomized_now(now),
            "kind": 13,
            "tags": [],
            "content": nip44.encrypt(json.dumps(rumor, ensure_ascii=False, separators=(",", ":")), ck),
        },
        sender_secret,
    )


def make_gift_wrap(recipient_hex: str, seal: dict[str, Any], now: int) -> dict[str, Any]:
    ephemeral = generate_secret()
    ck = nip44.get_conversation_key(ephemeral, recipient_hex)
    return finalize_event(
        {
            "created_at": _randomized_now(now),
            "kind": 1059,
            "tags": [["p", recipient_hex]],
            "content": nip44.encrypt(json.dumps(seal, ensure_ascii=False, separators=(",", ":")), ck),
        },
        ephemeral,
    )


def wrap_dm(sender_secret: bytes, recipient_hex: str, content: str, now: int | None = None) -> dict[str, Any]:
    now = int(now if now is not None else time.time())
    rumor = make_rumor(sender_secret, recipient_hex, content, now)
    return make_gift_wrap(recipient_hex, make_seal(sender_secret, recipient_hex, rumor, now), now)


def unwrap_dm(recipient_secret: bytes, wrap: dict[str, Any]) -> dict[str, Any]:
    """Open a gift wrap (used in tests and for local verification)."""
    seal = json.loads(nip44.decrypt(wrap["content"], nip44.get_conversation_key(recipient_secret, wrap["pubkey"])))
    rumor = json.loads(nip44.decrypt(seal["content"], nip44.get_conversation_key(recipient_secret, seal["pubkey"])))
    if rumor["pubkey"] != seal["pubkey"]:
        raise ValueError("rumor author does not match seal author")
    return rumor
