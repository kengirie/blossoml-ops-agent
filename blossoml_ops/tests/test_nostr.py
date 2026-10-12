import asyncio
import json
from pathlib import Path

import pytest

from blossoml_ops.notify import RECIPIENT_HEX, RECIPIENT_NPUB, nip44
from blossoml_ops.notify.keys import (
    bech32_decode,
    bech32_encode,
    finalize_event,
    generate_secret,
    parse_secret_key,
    verify_event,
    xonly_pubkey,
)
from blossoml_ops.notify.nip17 import unwrap_dm, wrap_dm
from blossoml_ops.notify.notifier import Notifier, NotifyLimitExceeded, format_message
from blossoml_ops.notify.relay import PublishResult, publish

VECTORS = json.loads((Path(__file__).parent / "data" / "nip44.vectors.json").read_text())["v2"]


# ---- NIP-44 official test vectors -----------------------------------------

@pytest.mark.parametrize("v", VECTORS["valid"]["get_conversation_key"])
def test_conversation_key(v):
    assert nip44.get_conversation_key(bytes.fromhex(v["sec1"]), v["pub2"]).hex() == v["conversation_key"]


def test_message_keys():
    ck = bytes.fromhex(VECTORS["valid"]["get_message_keys"]["conversation_key"])
    for k in VECTORS["valid"]["get_message_keys"]["keys"]:
        chacha_key, chacha_nonce, hmac_key = nip44.get_message_keys(ck, bytes.fromhex(k["nonce"]))
        assert (chacha_key.hex(), chacha_nonce.hex(), hmac_key.hex()) == (k["chacha_key"], k["chacha_nonce"], k["hmac_key"])


@pytest.mark.parametrize("v", VECTORS["valid"]["calc_padded_len"])
def test_padded_len(v):
    assert nip44.calc_padded_len(v[0]) == v[1]


@pytest.mark.parametrize("v", VECTORS["valid"]["encrypt_decrypt"])
def test_encrypt_decrypt(v):
    sec1, sec2 = bytes.fromhex(v["sec1"]), bytes.fromhex(v["sec2"])
    ck = nip44.get_conversation_key(sec1, xonly_pubkey(sec2))
    assert ck.hex() == v["conversation_key"]
    assert nip44.get_conversation_key(sec2, xonly_pubkey(sec1)) == ck
    payload = nip44.encrypt(v["plaintext"], ck, bytes.fromhex(v["nonce"]))
    assert payload == v["payload"]
    assert nip44.decrypt(payload, ck) == v["plaintext"]


@pytest.mark.parametrize("v", VECTORS["valid"]["encrypt_decrypt_long_msg"])
def test_encrypt_long(v):
    import hashlib

    plaintext = v["pattern"] * v["repeat"]
    ck = bytes.fromhex(v["conversation_key"])
    payload = nip44.encrypt(plaintext, ck, bytes.fromhex(v["nonce"]))
    assert hashlib.sha256(plaintext.encode()).hexdigest() == v["plaintext_sha256"]
    assert hashlib.sha256(payload.encode()).hexdigest() == v["payload_sha256"]
    assert nip44.decrypt(payload, ck) == plaintext


@pytest.mark.parametrize("length", VECTORS["invalid"]["encrypt_msg_lengths"])
def test_invalid_lengths(length):
    with pytest.raises(nip44.Nip44Error):
        nip44.encrypt("a" * length, b"\x01" * 32)


@pytest.mark.parametrize("v", VECTORS["invalid"]["decrypt"])
def test_invalid_decrypt(v):
    with pytest.raises(nip44.Nip44Error):
        nip44.decrypt(v["payload"], bytes.fromhex(v["conversation_key"]))


@pytest.mark.parametrize("v", VECTORS["invalid"]["get_conversation_key"])
def test_invalid_conversation_key(v):
    with pytest.raises(nip44.Nip44Error):
        nip44.get_conversation_key(bytes.fromhex(v["sec1"]), v["pub2"])


# ---- keys, events, NIP-17 ----------------------------------------------------

def test_recipient_constants_agree():
    hrp, data = bech32_decode(RECIPIENT_NPUB)
    assert hrp == "npub" and data.hex() == RECIPIENT_HEX
    assert bech32_encode("npub", bytes.fromhex(RECIPIENT_HEX)) == RECIPIENT_NPUB


def test_parse_secret_key():
    sk = generate_secret()
    assert parse_secret_key(bech32_encode("nsec", sk)) == sk
    assert parse_secret_key(sk.hex()) == sk
    with pytest.raises(ValueError):
        parse_secret_key(bech32_encode("npub", sk))
    with pytest.raises(Exception):
        parse_secret_key("ff" * 32)  # out of range


def test_event_signature():
    sk = generate_secret()
    ev = finalize_event({"created_at": 1, "kind": 1, "tags": [], "content": "こんにちは"}, sk)
    assert verify_event(ev)
    ev["content"] = "x"
    assert not verify_event(ev)


def test_gift_wrap_roundtrip():
    bot, operator = generate_secret(), generate_secret()
    wrap = wrap_dm(bot, xonly_pubkey(operator), "[info] blossoML: 異常なし", now=1_700_000_000)
    assert wrap["kind"] == 1059 and verify_event(wrap)
    assert wrap["tags"] == [["p", xonly_pubkey(operator)]]
    assert wrap["pubkey"] != xonly_pubkey(bot)  # signed by an ephemeral key
    assert wrap["created_at"] <= 1_700_000_000
    rumor = unwrap_dm(operator, wrap)
    assert rumor["kind"] == 14 and rumor["content"] == "[info] blossoML: 異常なし"
    assert rumor["pubkey"] == xonly_pubkey(bot)
    assert "sig" not in rumor
    with pytest.raises(Exception):
        unwrap_dm(generate_secret(), wrap)


def test_format_message():
    import datetime as dt

    text = format_message("warning", "ディスク", "空き 8%", "run-1", dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc))
    assert text == "[warning] blossoML: ディスク\n\n空き 8%\n\n(run: run-1, 2026-01-02T03:04:05+00:00)"


# ---- notifier ----------------------------------------------------------------

async def test_notifier_limits_and_undelivered(tmp_path):
    calls = []

    async def fake_publish(relays, event, **kw):
        calls.append(event)
        return [PublishResult(r, False, "error: down") for r in relays]

    n = Notifier(generate_secret(), "run", proxy=None, undelivered_path=tmp_path / "u.jsonl", publisher=fake_publish)
    out = await n.send("info", "t", "b")
    assert not out.ok and n.any_failed and not n.any_delivered
    rec = json.loads((tmp_path / "u.jsonl").read_text())
    assert rec["title"] == "t" and set(rec["relays"]) == set(n.relays)
    with pytest.raises(NotifyLimitExceeded):
        await n.send("info", "t2", "b")
    for _ in range(5):
        await n.send("warning", "w", "b")
    with pytest.raises(NotifyLimitExceeded):
        await n.send("critical", "c", "b")
    await n.send("critical", "harness", "b", harness=True)  # harness messages are not limited


# ---- relay protocol against a local server ------------------------------------

async def _serve(handler):
    from websockets.asyncio.server import serve

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


async def test_publish_ok_and_auth_flow():
    seen = {"auth": None}

    async def plain(ws):
        msg = json.loads(await ws.recv())
        await ws.send(json.dumps(["OK", msg[1]["id"], True, ""]))

    async def needs_auth(ws):
        await ws.send(json.dumps(["AUTH", "challenge-123"]))
        authed = False
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] == "AUTH":
                seen["auth"] = msg[1]
                authed = verify_event(msg[1]) and ["challenge", "challenge-123"] in msg[1]["tags"]
                await ws.send(json.dumps(["OK", msg[1]["id"], True, ""]))
            elif msg[0] == "EVENT":
                if authed:
                    await ws.send(json.dumps(["OK", msg[1]["id"], True, ""]))
                else:
                    await ws.send(json.dumps(["OK", msg[1]["id"], False, "auth-required: please auth"]))

    async def rejects(ws):
        msg = json.loads(await ws.recv())
        await ws.send(json.dumps(["OK", msg[1]["id"], False, "blocked: no"]))

    s1, u1 = await _serve(plain)
    s2, u2 = await _serve(needs_auth)
    s3, u3 = await _serve(rejects)
    try:
        bot = generate_secret()
        ev = wrap_dm(bot, xonly_pubkey(generate_secret()), "hello")
        results = await publish([u1, u2, u3], ev, auth_secret=bot, proxy=None, attempt_timeout=5, total_timeout=10)
        by = {r.relay: r for r in results}
        assert by[u1].accepted
        assert by[u2].accepted
        assert seen["auth"]["kind"] == 22242 and seen["auth"]["pubkey"] == xonly_pubkey(bot)
        assert not by[u3].accepted and by[u3].message.startswith("blocked")
    finally:
        for s in (s1, s2, s3):
            s.close()


async def test_publish_unreachable_relay_gives_up():
    bot = generate_secret()
    ev = wrap_dm(bot, xonly_pubkey(generate_secret()), "hello")
    results = await publish(["ws://127.0.0.1:9"], ev, auth_secret=bot, proxy=None, attempt_timeout=1, total_timeout=3)
    assert not results[0].accepted
