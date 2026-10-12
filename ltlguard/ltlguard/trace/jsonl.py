"""Trace files: one JSON event per line.

Event fields: ``ts``, ``session``, ``seq``, ``event`` (``pre`` | ``post`` |
``deny`` | ``stop`` | ``human`` | ...), and optionally ``tool``, ``args``,
``props``, ``result_summary``, ``monitor``. Callers must only pass values that
are safe to store (already checked, no secrets).

Files are named ``<prefix>-YYYYMMDD.jsonl`` (UTC). Old files are removed by age
and by total size.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class TraceWriter:
    def __init__(
        self,
        directory: str | Path,
        session: str,
        *,
        prefix: str = "trace",
        retention_days: int = 30,
        max_total_bytes: int = 50 * 1024 * 1024,
        max_event_bytes: int = 64 * 1024,
    ):
        self.directory = Path(directory)
        self.session = session
        self.prefix = prefix
        self.retention_days = retention_days
        self.max_total_bytes = max_total_bytes
        self.max_event_bytes = max_event_bytes
        self.seq = 0
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.rotate()

    def _path_for(self, now: _dt.datetime) -> Path:
        return self.directory / f"{self.prefix}-{now:%Y%m%d}.jsonl"

    def write(self, event: str, **fields: Any) -> dict:
        now = _utcnow()
        with self._lock:
            self.seq += 1
            record = {"ts": now.isoformat(timespec="milliseconds"), "session": self.session, "seq": self.seq, "event": event}
            record.update(fields)
            line = json.dumps(record, ensure_ascii=False, sort_keys=False, default=str)
            if len(line.encode("utf-8")) > self.max_event_bytes:
                record = {k: record[k] for k in ("ts", "session", "seq", "event")}
                record["truncated"] = True
                for k in ("tool", "props"):
                    if k in fields:
                        record[k] = fields[k]
                line = json.dumps(record, ensure_ascii=False, default=str)
            fd = os.open(self._path_for(now), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        return record

    def rotate(self) -> None:
        files = sorted(self.directory.glob(f"{self.prefix}-*.jsonl"))
        cutoff = _utcnow() - _dt.timedelta(days=self.retention_days)
        keep = []
        for f in files:
            try:
                day = _dt.datetime.strptime(f.stem.rsplit("-", 1)[1], "%Y%m%d").replace(tzinfo=_dt.timezone.utc)
            except (ValueError, IndexError):
                continue
            if day < cutoff:
                f.unlink(missing_ok=True)
            else:
                keep.append(f)
        total = sum(f.stat().st_size for f in keep)
        # Oldest first; never delete today's file.
        for f in keep[:-1]:
            if total <= self.max_total_bytes:
                break
            total -= f.stat().st_size
            f.unlink(missing_ok=True)


def read_events(paths: Iterable[str | Path], session: str | None = None) -> Iterator[Mapping[str, Any]]:
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if session is None or rec.get("session") == session:
                    yield rec
