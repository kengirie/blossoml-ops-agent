"""Output guard (design 6.3). Every tool result passes through ``check_output``
inside the tool function, right before it is returned to Claude.

This is one layer of invariant I1 (blob bytes never reach the API). Tools are
designed never to read blobs; this guard catches the case where that design
is broken anyway. Any doubt means rejection.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from typing import Any

MAX_OUTPUT_BYTES = 16 * 1024
MAX_DEPTH = 8
LONG_RUN = 200


class GuardViolation(Exception):
    """The output was discarded. ``kind`` names the reason; the content is never kept."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class OutputTooLarge(GuardViolation):
    def __init__(self, size: int):
        super().__init__("too_large")
        self.size = size


# Control characters other than \n and \t, C1 controls, DEL, and the Unicode
# replacement character (a sign of decoded binary data).
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f�]")

# Raw file signatures, as they would look after a latin-1 style decode, plus
# chunk names that only appear inside image/media containers.
_MAGIC_RAW = [
    ("png", re.compile("\x89PNG|IHDR|IDAT|IEND")),
    ("jpeg", re.compile("\xff\xd8\xff|JFIF|Exif")),
    ("gif", re.compile("GIF8[79]a")),
    ("webp", re.compile("RIFF.{4}WEBP|WEBPVP8", re.S)),
    ("mp4", re.compile("ftyp(?:isom|iso2|mp4|mp41|mp42|avc1|heic|heix|mif1|msf1|qt|M4V|M4A|3gp|dash|avif)")),
    ("pdf", re.compile("%PDF-")),
    ("svg", re.compile("<svg[\\s>]", re.I)),
    ("ogg", re.compile("OggS")),
    ("matroska", re.compile("\x1aE\xdf\xa3")),
    ("elf", re.compile("\x7fELF")),
    ("zip", re.compile("PK\x03\x04")),
    ("data_uri", re.compile("data:[a-z]+/[a-z0-9.+-]+;base64,", re.I)),
]

# Stringified representations of the same signatures: escaped, URL-encoded,
# hex, and base64 (for each of the three alignments where it is stable).
_MAGIC_STRINGIFIED = [
    ("png", re.compile(r"\\x89PNG|%89PNG|\\u0089PNG|89504e470d0a1a0a|iVBORw0KGgo", re.I)),
    ("jpeg", re.compile(r"\\xff\\xd8\\xff|%ff%d8%ff|\\u00ff\\u00d8\\u00ff|ffd8ff[e0-ef]|/9j/[0-9A-Za-z+/]{4}", re.I)),
    ("gif", re.compile(r"474946383[79]61|R0lGODlh|R0lGODdh")),
    ("webp", re.compile(r"52494646.{8}57454250|UklGR", re.I)),
    ("pdf", re.compile(r"255044462d|JVBERi0")),
    ("mp4", re.compile(r"66747970|ZnR5c|Z0eXA|mdHlw", re.I)),
    ("elf", re.compile(r"\\x7fELF|7f454c46|f0VMRg", re.I)),
    ("zip", re.compile(r"504b0304|UEsDBA", re.I)),
]

_HEX64_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{%d,}" % LONG_RUN)
# Candidate encoded runs. They are decoded and the bytes are checked for file
# signatures, which catches short fragments at any alignment.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")
_HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{16,}")
# Escaped bytes, e.g. Python's repr of bytes ("\x89\x00..."), JSON "\u00ff", or URL encoding.
_ESCAPED_BYTES_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2}|\\u00[0-9a-fA-F]{2}|%[0-9a-fA-F]{2}).{0,4}" * 8)


def _magic_in_bytes(data: bytes) -> str | None:
    text = data.decode("latin-1")
    for kind, rx in _MAGIC_RAW:
        if rx.search(text):
            return kind
    return None


def _decoded_base64(run: str):
    s = run.rstrip("=").replace("-", "+").replace("_", "/")
    for off in range(4):
        chunk = s[off:]
        chunk = chunk[: len(chunk) - len(chunk) % 4]
        if len(chunk) < 8:
            continue
        try:
            yield base64.b64decode(chunk, validate=True)
        except (binascii.Error, ValueError):
            continue


def _decoded_hex(run: str):
    for off in range(2):
        chunk = run[off:]
        chunk = chunk[: len(chunk) - len(chunk) % 2]
        try:
            yield bytes.fromhex(chunk)
        except ValueError:
            continue


def check_string(s: str) -> None:
    try:
        s.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise GuardViolation("invalid_utf8") from None
    if _CONTROL_RE.search(s):
        raise GuardViolation("control_char")
    for kind, rx in _MAGIC_RAW:
        if rx.search(s):
            raise GuardViolation(f"magic_{kind}")
    # 64-hex tokens (sha256, public keys) are allowed. They are removed before
    # the encoded-signature checks, so a hash that happens to contain e.g.
    # "89504e47" is not mistaken for an encoded PNG header.
    without_hex64 = _HEX64_RE.sub(" ", s)
    for kind, rx in _MAGIC_STRINGIFIED:
        if rx.search(without_hex64):
            raise GuardViolation(f"encoded_{kind}")
    for m in _B64_RUN_RE.finditer(without_hex64):
        for data in _decoded_base64(m.group(0)):
            kind = _magic_in_bytes(data)
            if kind:
                raise GuardViolation(f"base64_{kind}")
    for m in _HEX_RUN_RE.finditer(without_hex64):
        for data in _decoded_hex(m.group(0)):
            kind = _magic_in_bytes(data)
            if kind:
                raise GuardViolation(f"hex_{kind}")
    if _ESCAPED_BYTES_RE.search(s):
        raise GuardViolation("escaped_bytes")
    # Anything else that looks like a long base64/hex blob is rejected.
    if _LONG_RUN_RE.search(without_hex64):
        raise GuardViolation("long_encoded_run")


def _walk(value: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise GuardViolation("too_deep")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GuardViolation("non_finite_number")
        return
    if isinstance(value, str):
        check_string(value)
        return
    if isinstance(value, list):
        for v in value:
            _walk(v, depth + 1)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise GuardViolation("non_string_key")
            check_string(k)
            _walk(v, depth + 1)
        return
    # bytes, bytearray, memoryview, tuples, sets, objects: all rejected.
    raise GuardViolation(f"disallowed_type_{type(value).__name__}")


def check_output(value: Any) -> str:
    """Validate a tool result and return its JSON text. Raises GuardViolation."""
    if not isinstance(value, dict):
        raise GuardViolation("not_an_object")
    _walk(value, 0)
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    size = len(text.encode("utf-8"))
    if size > MAX_OUTPUT_BYTES:
        raise OutputTooLarge(size)
    return text


def args_for_trace(args: Any) -> Any:
    """Arguments as they may be written to the trace: unchanged if they pass the
    output guard, otherwise only the kind of violation."""
    try:
        check_output(dict(args))
    except GuardViolation as e:
        return {"_omitted": e.kind}
    except Exception:
        return {"_omitted": "unserializable"}
    return dict(args)
