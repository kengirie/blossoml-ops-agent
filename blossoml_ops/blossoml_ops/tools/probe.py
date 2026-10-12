"""http_probe: requests to the local blossoML over a fixed allowlist of targets.

There is no way to address any blob except the configured test blob. Blob
bodies are never returned; ``test_blob_get`` reports only whether the SHA-256
of the body matches.
"""

from __future__ import annotations

import hashlib
import http.client
import re
import ssl
import time
import urllib.parse

from blossoml_ops.tls import client_context
from blossoml_ops.tools.base import ToolError

TARGETS = ("root_get", "upload_head", "test_blob_head", "test_blob_get")
ROOT_BODY_CHARS = 300
MAX_TEST_BLOB_BYTES = 1024 * 1024
_HEADERS = ("content-type", "content-length", "x-reason", "accept-ranges", "access-control-allow-origin", "last-modified")
_CTRL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _request_for(ctx, target: str) -> tuple[str, str, dict]:
    if target == "root_get":
        return "GET", "/", {}
    if target == "upload_head":
        # BUD-06 upload requirements. Without Authorization the expected answer is 401.
        return "HEAD", "/upload", {"X-Content-Length": "1", "Content-Type": "text/plain"}
    sha = ctx.config.test_blob_sha256
    if not sha:
        raise ToolError("テスト用 blob の sha256 が設定されていません（test_blob_sha256）")
    return ("HEAD" if target == "test_blob_head" else "GET"), f"/{sha}", {}


def _connection(ctx, timeout: float):
    u = urllib.parse.urlsplit(ctx.config.blossoml_url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        context = client_context()
        if not ctx.config.tls_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def http_probe(ctx, target: str) -> dict:
    if target not in TARGETS:  # also validated before; defense in depth
        raise ToolError("許可されていない対象です")
    method, path, headers = _request_for(ctx, target)
    conn = _connection(ctx, timeout=10)
    t0 = time.monotonic()
    try:
        try:
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            return {"target": target, "reachable": False, "error_type": type(e).__name__, "elapsed_ms": round((time.monotonic() - t0) * 1000)}
        out = {
            "target": target,
            "reachable": True,
            "status": resp.status,
            "headers": {h: _CTRL_RE.sub(" ", v)[:200] for h in _HEADERS if (v := resp.getheader(h)) is not None},
        }
        if target == "root_get":
            body = resp.read(ROOT_BODY_CHARS * 4)
            text = body.decode("utf-8", "replace").replace("�", "?")
            out["body_head"] = _CTRL_RE.sub(" ", text)[:ROOT_BODY_CHARS]
        elif target == "test_blob_get":
            h = hashlib.sha256()
            size = 0
            truncated = False
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_TEST_BLOB_BYTES:
                    truncated = True
                    break
                h.update(chunk)
            out["body_bytes"] = size
            out["sha256_match"] = (not truncated) and resp.status == 200 and h.hexdigest() == ctx.config.test_blob_sha256
        out["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        return out
    finally:
        conn.close()
