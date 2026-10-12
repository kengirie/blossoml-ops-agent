"""Seed the lab's blossoML through its real API (BUD-02 uploads and deletes),
then add two things for the agent to notice. Prints the test blob's sha256.

Runs as the ubuntu user (owner of the blossoML directory).
"""

import base64
import hashlib
import http.client
import json
import os
import time

from blossoml_ops.notify.keys import finalize_event, generate_secret

DATA = "/home/ubuntu/blossoML/data"
TEST_BLOB = b"blossoml-ops lab test blob\n"


def auth(secret: bytes, action: str, sha: str) -> str:
    now = int(time.time())
    ev = finalize_event(
        {"created_at": now - 5, "kind": 24242, "content": action,
         "tags": [["t", action], ["x", sha], ["expiration", str(now + 600)]]},
        secret,
    )
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def request(method: str, path: str, body: bytes = b"", headers: dict | None = None) -> int:
    conn = http.client.HTTPConnection("localhost", 8082, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    status = conn.getresponse().status
    conn.close()
    return status


def upload(secret: bytes, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    status = request("PUT", "/upload", data, {"Authorization": auth(secret, "upload", sha), "Content-Type": "text/plain"})
    if status not in (200, 201):
        raise SystemExit(f"upload failed: HTTP {status}")
    return sha


def main() -> None:
    os.umask(0o027)  # like blossoML under UMask=0027
    operator, alice, bob = generate_secret(), generate_secret(), generate_secret()
    test_sha = upload(operator, TEST_BLOB)
    for i in range(4):
        upload(alice, f"note {i} from alice\n".encode())
    for i in range(2):
        upload(bob, f"note {i} from bob\n".encode())
    doomed = upload(bob, b"this one gets deleted\n")
    if request("DELETE", f"/{doomed}", headers={"Authorization": auth(bob, "delete", doomed)}) not in (200, 204):
        raise SystemExit("delete failed")
    request("GET", "/")  # a little ordinary traffic
    request("GET", "/" + "0" * 64)  # a 404

    # Things the daily check should report as warnings:
    # an upload leftover older than 24 hours, and a file the DB does not know.
    stale = os.path.join(DATA, "tmp_123456_654321")
    with open(stale, "wb") as f:
        f.write(b"partial upload")
    old = time.time() - 30 * 3600
    os.utime(stale, (old, old))
    orphan = b"file without a database row\n"
    with open(os.path.join(DATA, hashlib.sha256(orphan).hexdigest()), "wb") as f:
        f.write(orphan)

    print(test_sha)


if __name__ == "__main__":
    main()
