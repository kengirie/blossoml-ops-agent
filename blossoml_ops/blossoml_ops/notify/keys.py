"""secp256k1 keys, bech32 (NIP-19), and NIP-01 event ids/signatures."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from coincurve import PrivateKey, PublicKey, PublicKeyXOnly

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits: int, tobits: int, pad: bool) -> list[int]:
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise ValueError("invalid data")
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("invalid padding")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    values = _convertbits(data, 8, 5, True)
    polymod = _polymod(_hrp_expand(hrp) + values + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in values + checksum)


def bech32_decode(s: str) -> tuple[str, bytes]:
    if s.lower() != s and s.upper() != s:
        raise ValueError("mixed case")
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        raise ValueError("invalid bech32")
    hrp, data = s[:pos], s[pos + 1:]
    try:
        values = [_CHARSET.index(c) for c in data]
    except ValueError:
        raise ValueError("invalid character") from None
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("bad checksum")
    return hrp, bytes(_convertbits(values[:-6], 5, 8, False))


def parse_secret_key(text: str) -> bytes:
    """Accept an ``nsec1...`` string or 64 hex characters."""
    text = text.strip()
    if text.startswith("nsec1"):
        hrp, raw = bech32_decode(text)
        if hrp != "nsec":
            raise ValueError("not an nsec")
    else:
        raw = bytes.fromhex(text)
    if len(raw) != 32:
        raise ValueError("secret key must be 32 bytes")
    PrivateKey(raw)  # validates range
    return raw


def xonly_pubkey(secret: bytes) -> str:
    return PublicKeyXOnly.from_secret(secret).format().hex()


def generate_secret() -> bytes:
    while True:
        k = os.urandom(32)
        try:
            PrivateKey(k)
            return k
        except Exception:
            continue


def ecdh_x(secret: bytes, pubkey_xonly_hex: str) -> bytes:
    """Unhashed x coordinate of secret * P (as NIP-44 requires)."""
    pub = PublicKey(b"\x02" + bytes.fromhex(pubkey_xonly_hex))
    return pub.multiply(secret).format(compressed=True)[1:]


def event_id(ev: dict[str, Any]) -> str:
    serialized = json.dumps(
        [0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def finalize_event(ev: dict[str, Any], secret: bytes) -> dict[str, Any]:
    ev = dict(ev)
    ev["pubkey"] = xonly_pubkey(secret)
    ev["id"] = event_id(ev)
    ev["sig"] = PrivateKey(secret).sign_schnorr(bytes.fromhex(ev["id"]), os.urandom(32)).hex()
    return ev


def verify_event(ev: dict[str, Any]) -> bool:
    if event_id(ev) != ev.get("id"):
        return False
    try:
        return PublicKeyXOnly(bytes.fromhex(ev["pubkey"])).verify(bytes.fromhex(ev["sig"]), bytes.fromhex(ev["id"]))
    except Exception:
        return False
