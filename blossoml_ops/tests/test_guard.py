"""Output guard (design 6.3, acceptance test for I1): images, base64 images and
binary data must always be rejected."""

import base64
import json
import os
import struct
import zlib

import pytest

from blossoml_ops.guard import MAX_OUTPUT_BYTES, GuardViolation, OutputTooLarge, check_output


def tiny_png() -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"\x00\xff\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


SAMPLES = {
    "png": tiny_png(),
    "jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + os.urandom(64) + b"\xff\xd9",
    "gif": b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04" + os.urandom(32),
    "webp": b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00" + os.urandom(24),
    "mp4": b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + os.urandom(32),
    "pdf": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n",
    "random": os.urandom(256),
}


def latin1(b: bytes) -> str:
    return b.decode("latin-1")


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_raw_bytes_rejected(name):
    with pytest.raises(GuardViolation):
        check_output({"x": SAMPLES[name]})


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_latin1_decoded_rejected(name):
    with pytest.raises(GuardViolation):
        check_output({"x": latin1(SAMPLES[name])})


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_utf8_replace_decoded_rejected(name):
    with pytest.raises(GuardViolation):
        check_output({"x": SAMPLES[name].decode("utf-8", "replace")})


@pytest.mark.parametrize("name", sorted(SAMPLES))
@pytest.mark.parametrize("prefix", ["", "a", "ab"])  # all three base64 alignments
def test_base64_rejected(name, prefix):
    data = prefix.encode() + SAMPLES[name]
    b64 = base64.b64encode(data).decode()
    with pytest.raises(GuardViolation):
        check_output({"x": f"blob: {b64}"})


@pytest.mark.parametrize("name", ["png", "jpeg", "gif", "pdf", "mp4", "webp"])
def test_short_base64_header_rejected(name):
    # Even a short fragment of the beginning of an image is rejected.
    b64 = base64.b64encode(SAMPLES[name][:12]).decode()
    with pytest.raises(GuardViolation):
        check_output({"x": b64})


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_hex_rejected(name):
    with pytest.raises(GuardViolation):
        check_output({"x": SAMPLES[name].hex()})


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_escaped_repr_rejected(name):
    with pytest.raises(GuardViolation):
        check_output({"x": repr(SAMPLES[name])})


def test_data_uri_rejected():
    with pytest.raises(GuardViolation):
        check_output({"x": "data:image/png;base64,AAAA"})


def test_svg_rejected():
    with pytest.raises(GuardViolation):
        check_output({"x": '<svg xmlns="http://www.w3.org/2000/svg"></svg>'})


def test_nested_and_keys_checked():
    with pytest.raises(GuardViolation):
        check_output({"a": [{"b": ["ok", latin1(SAMPLES["png"])]}]})
    with pytest.raises(GuardViolation):
        check_output({latin1(SAMPLES["gif"]): 1})


@pytest.mark.parametrize("value", [b"x", bytearray(b"x"), memoryview(b"x"), (1, 2), {1, 2}, object(), float("nan"), float("inf")])
def test_disallowed_types(value):
    with pytest.raises(GuardViolation):
        check_output({"x": value})


def test_control_chars_rejected():
    for ch in ["\x00", "\x01", "\x1b", "\r", "\x7f", "\x85"]:
        with pytest.raises(GuardViolation):
            check_output({"x": f"a{ch}b"})


def test_long_base64_run_rejected():
    with pytest.raises(GuardViolation):
        check_output({"x": "A" * 200})


def test_too_large():
    with pytest.raises(OutputTooLarge):
        check_output({"x": ["word " * 50] * (MAX_OUTPUT_BYTES // 200)})


def test_normal_output_allowed():
    sha = "f" * 64
    hashes = [os.urandom(32).hex() for _ in range(40)]
    out = {
        "status": 200,
        "elapsed_ms": 12.5,
        "ok": True,
        "none": None,
        "line": f"Response: GET /{sha} -> 200 [content-type: image/png; content-length: 1234]",
        "multi": "line1\nline2\tTabbed",
        "japanese": "異常なし。ディスク空き 42%",
        "hashes": hashes,
        "joined": " ".join(hashes),
        "path": "/home/ubuntu/blossoML/data/" + sha,
        "tos": "Terms of Service\nBy uploading you agree...",
    }
    assert json.loads(check_output(out)) == out


def test_sha256_with_magic_like_hex_allowed():
    # A sha256 that happens to contain the hex of a PNG header is still a sha256.
    sha = "0" * 8 + "89504e470d0a1a0a" + "0" * 40
    assert len(sha) == 64
    check_output({"sha256": sha, "line": f"Upload successful: {sha} (10 bytes, image/png)"})
