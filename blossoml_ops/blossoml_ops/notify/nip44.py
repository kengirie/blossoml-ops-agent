"""NIP-44 version 2 encryption. Verified against the official test vectors
(tests/data/nip44.vectors.json)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from blossoml_ops.notify.keys import ecdh_x

VERSION = 2
MIN_PLAINTEXT = 1
MAX_PLAINTEXT = 65535


class Nip44Error(ValueError):
    pass


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out = b""
    t = b""
    i = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def get_conversation_key(secret: bytes, pubkey_xonly_hex: str) -> bytes:
    try:
        shared_x = ecdh_x(secret, pubkey_xonly_hex)
    except Exception as e:
        raise Nip44Error(f"invalid key: {type(e).__name__}") from None
    return _hkdf_extract(b"nip44-v2", shared_x)


def get_message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    if len(conversation_key) != 32:
        raise Nip44Error("invalid conversation key length")
    if len(nonce) != 32:
        raise Nip44Error("invalid nonce length")
    keys = _hkdf_expand(conversation_key, nonce, 76)
    return keys[0:32], keys[32:44], keys[44:76]


def calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 0:
        raise Nip44Error("invalid plaintext length")
    if unpadded_len <= 32:
        return 32
    next_power = 1 << ((unpadded_len - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded_len - 1) // chunk + 1)


def _pad(plaintext: str) -> bytes:
    raw = plaintext.encode("utf-8")
    n = len(raw)
    if not MIN_PLAINTEXT <= n <= MAX_PLAINTEXT:
        raise Nip44Error("invalid plaintext length")
    return struct.pack(">H", n) + raw + b"\x00" * (calc_padded_len(n) - n)


def _unpad(padded: bytes) -> str:
    n = struct.unpack(">H", padded[:2])[0]
    raw = padded[2:2 + n]
    if n == 0 or len(raw) != n or len(padded) != 2 + calc_padded_len(n):
        raise Nip44Error("invalid padding")
    return raw.decode("utf-8")


def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    # cryptography's ChaCha20 takes a 16-byte nonce: 32-bit LE counter (0) || 96-bit nonce.
    cipher = Cipher(algorithms.ChaCha20(key, b"\x00\x00\x00\x00" + nonce12), mode=None)
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def _hmac_aad(key: bytes, message: bytes, aad: bytes) -> bytes:
    if len(aad) != 32:
        raise Nip44Error("AAD associated data must be 32 bytes")
    return hmac.new(key, aad + message, hashlib.sha256).digest()


def encrypt(plaintext: str, conversation_key: bytes, nonce: bytes | None = None) -> str:
    nonce = nonce if nonce is not None else os.urandom(32)
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    ciphertext = _chacha20(chacha_key, chacha_nonce, _pad(plaintext))
    mac = _hmac_aad(hmac_key, ciphertext, nonce)
    return base64.b64encode(bytes([VERSION]) + nonce + ciphertext + mac).decode("ascii")


def decrypt(payload: str, conversation_key: bytes) -> str:
    plen = len(payload)
    if plen == 0 or payload[0] == "#":
        raise Nip44Error("unknown version")
    if plen < 132 or plen > 87472:
        raise Nip44Error("invalid payload size")
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        raise Nip44Error("invalid base64") from None
    dlen = len(data)
    if dlen < 99 or dlen > 65603:
        raise Nip44Error("invalid data size")
    if data[0] != VERSION:
        raise Nip44Error("unknown version")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    if not hmac.compare_digest(_hmac_aad(hmac_key, ciphertext, nonce), mac):
        raise Nip44Error("invalid MAC")
    return _unpad(_chacha20(chacha_key, chacha_nonce, ciphertext))
