"""db_report: named, fixed queries on blossom.db opened read-only.

The blob_reports table is never queried.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
import urllib.parse

from blossoml_ops.tools.base import ToolError

REPORTS = ("blob_counts_by_status", "recent_uploads_count", "owner_counts", "consistency")
MAX_LISTED = 20
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _connect(path) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as e:
        raise ToolError(f"DB を読み取り専用で開けません（{type(e).__name__}）") from None


def _mask(pk: str, mask: bool) -> str:
    return pk[:8] + "…" if mask else pk


def db_report(ctx, name: str) -> dict:
    now = int(time.time())
    conn = _connect(ctx.config.db_path)
    try:
        if name == "blob_counts_by_status":
            rows = conn.execute(
                "SELECT status, COUNT(*), COALESCE(SUM(size), 0) FROM blobs GROUP BY status ORDER BY status"
            ).fetchall()
            current = {s: {"count": c, "bytes": b} for s, c, b in rows}
            out = {"by_status": current}
            prev = ctx.history.previous("blob_counts_by_status")
            if prev:
                out["previous_run"] = prev
            ctx.history.record("blob_counts_by_status", current)
            return out
        if name == "recent_uploads_count":
            def count(lo, hi):
                return conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM blobs WHERE uploaded_at >= ? AND uploaded_at < ?", (lo, hi)
                ).fetchone()

            d = 86400
            c24, b24 = count(now - d, now + 60)
            cp, bp = count(now - 2 * d, now - d)
            c7, b7 = count(now - 7 * d, now + 60)
            return {
                "last_24h": {"count": c24, "bytes": b24},
                "previous_24h": {"count": cp, "bytes": bp},
                "last_7d": {"count": c7, "bytes": b7},
            }
        if name == "owner_counts":
            total_rows, distinct = conn.execute("SELECT COUNT(*), COUNT(DISTINCT pubkey) FROM blob_owners").fetchone()
            new_24h = conn.execute(
                "SELECT COUNT(DISTINCT pubkey) FROM blob_owners WHERE added_at >= ?", (now - 86400,)
            ).fetchone()[0]
            orphans = conn.execute(
                "SELECT COUNT(*) FROM blobs b WHERE b.status = 'stored' "
                "AND NOT EXISTS (SELECT 1 FROM blob_owners o WHERE o.sha256 = b.sha256)"
            ).fetchone()[0]
            top = conn.execute(
                "SELECT pubkey, COUNT(*) AS n FROM blob_owners WHERE added_at >= ? GROUP BY pubkey ORDER BY n DESC LIMIT 5",
                (now - 7 * 86400,),
            ).fetchall()
            return {
                "owner_rows": total_rows,
                "distinct_owners": distinct,
                "owners_active_last_24h": new_24h,
                "stored_blobs_without_owner": orphans,
                "top_uploaders_last_7d": [{"pubkey": _mask(pk, ctx.config.mask_pubkeys), "blobs": n} for pk, n in top],
            }
        if name == "consistency":
            stored = {r[0] for r in conn.execute("SELECT sha256 FROM blobs WHERE status = 'stored'")}
            known = {r[0]: r[1] for r in conn.execute("SELECT sha256, status FROM blobs")}
            try:
                with os.scandir(ctx.config.data_dir) as it:
                    files = {e.name for e in it if _SHA_RE.match(e.name) and e.is_file(follow_symlinks=False)}
            except OSError:
                raise ToolError("data ディレクトリを一覧できません") from None
            missing = sorted(stored - files)
            untracked = sorted(files - set(known))
            not_stored_but_present = sorted(s for s in files & set(known) if known[s] != "stored")
            return {
                "stored_in_db": len(stored),
                "blob_files": len(files),
                "stored_but_file_missing": {"count": len(missing), "sha256": missing[:MAX_LISTED]},
                "file_without_db_row": {"count": len(untracked), "sha256": untracked[:MAX_LISTED]},
                "file_present_but_status_not_stored": {"count": len(not_stored_but_present), "sha256": not_stored_but_present[:MAX_LISTED]},
            }
        raise ToolError("未知のレポート名です")
    except sqlite3.Error as e:
        raise ToolError(f"クエリに失敗しました（{type(e).__name__}）") from None
    finally:
        conn.close()
