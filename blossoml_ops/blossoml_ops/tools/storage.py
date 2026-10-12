"""disk_usage, storage_audit, deploy_info. Blob files are only listed and
stat()-ed, never opened."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time

from blossoml_ops.tools.base import ToolError

SHA256_NAME_RE = re.compile(r"^[0-9a-f]{64}$")
TMP_NAME_RE = re.compile(r"^tmp_\d+_\d+$")
MAX_TMP_LISTED = 50


def _dir_total(path) -> tuple[int, int]:
    total = count = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
                    count += 1
    except FileNotFoundError:
        raise ToolError(f"ディレクトリがありません: {path.name}") from None
    return total, count


def disk_usage(ctx) -> dict:
    cfg = ctx.config
    try:
        vfs = os.statvfs(cfg.blossoml_dir)
    except OSError:
        raise ToolError("blossoML のディレクトリに対して statvfs できません") from None
    total = vfs.f_blocks * vfs.f_frsize
    free = vfs.f_bavail * vfs.f_frsize
    data_bytes, data_files = _dir_total(cfg.data_dir)
    db_bytes, _ = _dir_total(cfg.data_dir.parent / "db")
    current = {
        "filesystem_total_bytes": total,
        "filesystem_free_bytes": free,
        "filesystem_free_percent": round(100.0 * free / total, 2) if total else None,
        "data_dir_bytes": data_bytes,
        "data_dir_files": data_files,
        "db_dir_bytes": db_bytes,
    }
    out = dict(current)
    prev = ctx.history.previous("disk_usage")
    if prev:
        out["previous_run"] = prev
    ctx.history.record("disk_usage", current)
    return out


def storage_audit(ctx) -> dict:
    now = time.time()
    blobs = tmp_count = other = non_regular = world_readable = group_writable = 0
    tmp_files = []
    try:
        it = os.scandir(ctx.config.data_dir)
    except OSError:
        raise ToolError("data ディレクトリを一覧できません") from None
    with it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                non_regular += 1
                continue
            if st.st_mode & stat.S_IROTH:
                world_readable += 1
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                group_writable += 1
            if SHA256_NAME_RE.match(e.name):
                blobs += 1
            elif TMP_NAME_RE.match(e.name):
                tmp_count += 1
                tmp_files.append({"name": e.name, "age_hours": round((now - st.st_mtime) / 3600, 1), "size": st.st_size})
            else:
                other += 1  # names are not reported: only the count
    tmp_files.sort(key=lambda t: -t["age_hours"])
    out = {
        "blob_files": blobs,
        "tmp_files_count": tmp_count,
        "tmp_files": tmp_files[:MAX_TMP_LISTED],
        "nonconforming_names": other,
        "non_regular_entries": non_regular,
        "world_readable_files": world_readable,
        "group_or_world_writable_files": group_writable,
    }
    if tmp_count > MAX_TMP_LISTED:
        out["tmp_files_truncated"] = True
    return out


def _file_info(path) -> dict:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"exists": False}
    h = hashlib.sha256()
    with open(path, "rb") as f:  # the server binary, not a blob
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {
        "exists": True,
        "size": st.st_size,
        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        "sha256": h.hexdigest(),
    }


def deploy_info(ctx) -> dict:
    d = ctx.config.blossoml_dir
    from blossoml_ops import __version__

    return {
        "main_exe": _file_info(d / "main.exe"),
        "main_exe_backup": _file_info(d / "main.exe.backup"),
        "ops_agent_version": __version__,
    }
