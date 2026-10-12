import os
import sqlite3
import time
from pathlib import Path

import pytest

from blossoml_ops.config import parse_config

SCHEMA = """
CREATE TABLE blobs (
  sha256 TEXT(64) PRIMARY KEY,
  uploaded_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'stored' CHECK (status IN ('stored','deleted','quarantined')),
  remote_url TEXT, mime_type TEXT, size INTEGER);
CREATE TABLE blob_owners (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT(64) NOT NULL, pubkey TEXT(64) NOT NULL,
  added_at INTEGER NOT NULL, UNIQUE(sha256, pubkey));
CREATE TABLE blob_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, sha256 TEXT, reporter_pubkey TEXT,
  report_type TEXT, content TEXT, e_tag TEXT, p_tag TEXT, raw_event_json TEXT, event_created_at INTEGER, received_at INTEGER);
"""


def h(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def fake_server(tmp_path):
    """A fake blossoML working directory with blobs, tmp files and a DB."""
    base = tmp_path / "blossoML"
    data, db = base / "data", base / "db"
    data.mkdir(parents=True)
    db.mkdir()
    now = int(time.time())
    for n in (1, 2, 3):
        p = data / h(n)
        p.write_bytes(b"\x89PNG\r\n\x1a\n not for reading")
        os.chmod(p, 0o640)
    (data / h(99)).write_bytes(b"untracked")  # file without DB row
    os.chmod(data / h(99), 0o644)            # world-readable
    old_tmp = data / "tmp_1_2"
    old_tmp.write_bytes(b"x" * 10)
    os.utime(old_tmp, (now - 30 * 3600, now - 30 * 3600))
    (data / "tmp_3_4").write_bytes(b"y")
    (data / "weird.name").write_bytes(b"z")
    (base / "main.exe").write_bytes(b"binary-v2")
    (base / "main.exe.backup").write_bytes(b"binary-v1")
    conn = sqlite3.connect(db / "blossom.db")
    conn.executescript(SCHEMA)
    rows = [
        (h(1), now - 100, "stored", 100), (h(2), now - 90000, "stored", 200), (h(3), now - 200, "deleted", 300),
        (h(4), now - 300, "stored", 400),  # stored but file missing
    ]
    conn.executemany("INSERT INTO blobs (sha256, uploaded_at, status, size) VALUES (?, ?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO blob_owners (sha256, pubkey, added_at) VALUES (?, ?, ?)",
        [(h(1), "a" * 64, now - 100), (h(2), "b" * 64, now - 90000)],
    )
    conn.execute("INSERT INTO blob_reports (sha256) VALUES (?)", (h(1),))
    conn.commit()
    conn.close()
    return base


@pytest.fixture
def config(tmp_path, fake_server):
    def make(**overrides):
        raw = {
            "blossoml": {"dir": str(fake_server), "url": "http://localhost:8082"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "test_blob_sha256": h(1),
        }
        raw.update(overrides)
        (tmp_path / "state").mkdir(exist_ok=True)
        return parse_config(raw)

    return make
