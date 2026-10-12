"""read_logs: aggregate blossoML's journal entries.

Only entries with ``_SYSTEMD_UNIT=<blossoml unit>`` are read. The result is
counts, plus a few short, sanitized sample lines for errors.

Lines about reports (BUD-09, ``/report``) are skipped entirely: this agent
does not deal with reports.
"""

from __future__ import annotations

import collections
import json
import re
import time
from dataclasses import dataclass
from typing import Iterable, Iterator

from blossoml_ops.guard import GuardViolation, check_string

KINDS = ("errors", "uploads", "deletes", "mirror", "status_codes", "all_summary")
MAX_SAMPLES = 20
MAX_SAMPLE_LEN = 300

_REPORT_RE = re.compile(r"Report received|persist report|/report\b", re.I)
_RESPONSE_RE = re.compile(r"^Response: (\S+) (\S+) -> (\d{3})")
_REQUEST_RE = re.compile(r"^Request: (\S+) (\S+)")
_UPLOAD_OK_RE = re.compile(r"^Upload successful: [0-9a-f]{64} \((\d+) bytes")
_MASK_RE = re.compile(r"(?<=\bby )[0-9a-f]{64}|(?<=/list/)[0-9a-f]{64}|(?<=reporter=)[0-9a-f]{64}")
_CTRL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# (category, pattern) for error lines, in order of precedence.
ERROR_PATTERNS = [
    ("save_failed", re.compile(r"^Save failed")),
    ("mirror_failed", re.compile(r"^Mirror failed")),
    ("sha256_mismatch", re.compile(r"^SHA256 mismatch")),
    ("mime_policy_violation", re.compile(r"^MIME type policy violation")),
    ("unlink_failed", re.compile(r"^WARNING: Failed to unlink")),
    ("delete_failed", re.compile(r"^Delete failed")),
    ("ssrf_blocked", re.compile(r"^SSRF blocked")),
    ("db_init_failed", re.compile(r"^Database initialization failed")),
    ("other_error", re.compile(r"error|failed|exception|fatal|panic|uncaught", re.I)),
]


@dataclass
class Entry:
    ts: float          # seconds since the epoch
    message: str | None  # None if the message was not valid UTF-8


def normalize(message: str) -> str:
    # Eio.traceln prefixes lines with "+".
    return message[1:] if message.startswith("+") else message


def classify_error(msg: str) -> str | None:
    for cat, rx in ERROR_PATTERNS:
        if rx.search(msg):
            return cat
    return None


class Counts:
    def __init__(self) -> None:
        self.lines = 0
        self.undecodable = 0
        self.errors: collections.Counter = collections.Counter()
        self.status: collections.Counter = collections.Counter()
        self.status_by_method: collections.Counter = collections.Counter()
        self.put_upload_status: collections.Counter = collections.Counter()
        self.events: collections.Counter = collections.Counter()
        self.upload_bytes = 0

    def add(self, msg: str) -> str | None:
        """Count one message. Returns its error category, if any."""
        self.lines += 1
        m = _RESPONSE_RE.match(msg)
        if m:
            method, target, status = m.groups()
            self.status[status] += 1
            self.status_by_method[f"{method} {status[0]}xx"] += 1
            if method == "PUT" and target == "/upload":
                self.put_upload_status[status] += 1
            if status.startswith("5"):
                self.errors["http_5xx"] += 1
                return "http_5xx"
            return None
        if _REQUEST_RE.match(msg) or msg.startswith(("Path parts:", "Upload response:")):
            return None
        m = _UPLOAD_OK_RE.match(msg)
        if m:
            self.events["upload_ok"] += 1
            self.upload_bytes += int(m.group(1))
            return None
        if msg.startswith("Mirror successful"):
            self.events["mirror_ok"] += 1
            return None
        if msg.startswith("Delete request"):
            self.events["delete_request"] += 1
            return None
        if msg.startswith("Delete successful"):
            self.events["delete_ok"] += 1
            return None
        if msg.startswith("Starting Blossom server"):
            self.events["server_start"] += 1
            return None
        cat = classify_error(msg)
        if cat:
            self.errors[cat] += 1
        return cat


def sanitize_sample(msg: str, mask_pubkeys: bool) -> str | None:
    s = _CTRL_RE.sub(" ", msg)
    if mask_pubkeys:
        s = _MASK_RE.sub(lambda m: m.group(0)[:8] + "…", s)
    if len(s) > MAX_SAMPLE_LEN:
        s = s[:MAX_SAMPLE_LEN] + "…"
    try:
        check_string(s)
    except GuardViolation:
        return None
    return s


def summarize(
    entries: Iterable[Entry],
    kind: str,
    since_hours: int,
    now: float,
    *,
    deploy_time: float | None = None,
    mask_pubkeys: bool = False,
) -> dict:
    """Aggregate entries from the last ``2 * since_hours`` hours into the
    current window and the previous window of the same length."""
    start = now - since_hours * 3600
    cur, prev = Counts(), Counts()
    before_deploy, after_deploy = Counts(), Counts()
    split = deploy_time is not None and start < deploy_time <= now
    samples: list[dict] = []
    samples_dropped = 0
    sample_cats = {
        "errors": None,  # any error category
        "mirror": {"mirror_failed", "ssrf_blocked"},
        "deletes": {"delete_failed", "unlink_failed"},
        "uploads": {"save_failed", "sha256_mismatch", "mime_policy_violation"},
    }

    for e in entries:
        if e.message is None:
            (cur if e.ts >= start else prev).undecodable += 1
            continue
        msg = normalize(e.message)
        if _REPORT_RE.search(msg):
            continue
        if e.ts < start:
            prev.add(msg)
            continue
        cat = cur.add(msg)
        if split:
            (after_deploy if e.ts >= deploy_time else before_deploy).add(msg)  # type: ignore[operator]
        if cat and kind in sample_cats and (sample_cats[kind] is None or cat in sample_cats[kind]):
            if len(samples) < MAX_SAMPLES:
                s = sanitize_sample(msg, mask_pubkeys)
                if s is None:
                    samples_dropped += 1
                else:
                    samples.append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e.ts)), "category": cat, "line": s})

    def section(c: Counts) -> dict:
        if kind == "errors":
            return {"lines": c.lines, "error_counts": dict(c.errors)}
        if kind == "uploads":
            return {
                "upload_ok": c.events["upload_ok"],
                "upload_bytes": c.upload_bytes,
                "put_upload_status": dict(c.put_upload_status),
                "save_failed": c.errors["save_failed"],
                "sha256_mismatch": c.errors["sha256_mismatch"],
                "mime_policy_violation": c.errors["mime_policy_violation"],
            }
        if kind == "deletes":
            return {
                "delete_request": c.events["delete_request"],
                "delete_ok": c.events["delete_ok"],
                "delete_failed": c.errors["delete_failed"],
                "unlink_failed": c.errors["unlink_failed"],
            }
        if kind == "mirror":
            return {"mirror_ok": c.events["mirror_ok"], "mirror_failed": c.errors["mirror_failed"], "ssrf_blocked": c.errors["ssrf_blocked"]}
        if kind == "status_codes":
            return {"by_status": dict(c.status), "by_method_class": dict(c.status_by_method)}
        return {
            "lines": c.lines,
            "undecodable_lines": c.undecodable,
            "server_starts": c.events["server_start"],
            "events": dict(c.events),
            "upload_bytes": c.upload_bytes,
            "error_counts": dict(c.errors),
            "by_status": dict(c.status),
        }

    out = {
        "kind": kind,
        "window_hours": since_hours,
        "current_window": section(cur),
        "previous_window": section(prev),
    }
    if cur.undecodable:
        out["undecodable_lines"] = cur.undecodable
    if split:
        out["split_at_last_deploy"] = {
            "deploy_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(deploy_time)),  # type: ignore[arg-type]
            "before": section(before_deploy),
            "after": section(after_deploy),
        }
    if kind in sample_cats:
        out["samples"] = samples
        if samples_dropped:
            out["samples_dropped"] = samples_dropped
    return out


def degrade(result: dict) -> dict:
    """Fallback when the result is too large: drop samples and per-key detail."""
    r = {k: v for k, v in result.items() if k not in ("samples", "split_at_last_deploy")}
    r["degraded"] = "出力が大きすぎたため、サンプル行とデプロイ前後の内訳を省略しました"
    return r


# ---- journal sources -------------------------------------------------------

def journal_entries(unit: str, since_epoch: float, executor=None, max_entries: int = 2_000_000) -> Iterator[Entry]:
    """Read entries of ``unit`` since ``since_epoch``. Uses python-systemd if
    available, otherwise ``journalctl -o json`` with a fixed argv."""
    try:
        from systemd import journal  # type: ignore[import-not-found]
    except ImportError:
        journal = None
    if journal is not None:
        reader = journal.Reader()
        try:
            reader.add_match(_SYSTEMD_UNIT=unit)
            reader.seek_realtime(since_epoch)
            for i, e in enumerate(reader):
                if i >= max_entries:
                    break
                msg = e.get("MESSAGE")
                ts = e["__REALTIME_TIMESTAMP"].timestamp()
                if isinstance(msg, bytes):
                    try:
                        msg = msg.decode("utf-8")
                    except UnicodeDecodeError:
                        msg = None
                yield Entry(ts, msg if isinstance(msg, str) or msg is None else str(msg))
        finally:
            reader.close()
        return
    if executor is None:
        raise RuntimeError("no journal reader available")
    argv = [
        "/usr/bin/journalctl", f"_SYSTEMD_UNIT={unit}", f"--since=@{int(since_epoch)}",
        "--output=json", "--output-fields=MESSAGE", "--no-pager", "--quiet",
    ]
    for raw in executor.stream_lines(argv, timeout=120, max_lines=max_entries):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):  # non-UTF-8 messages are byte arrays
            try:
                msg = bytes(msg).decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                msg = None
        try:
            ts = int(rec["__REALTIME_TIMESTAMP"]) / 1e6
        except (KeyError, ValueError):
            continue
        yield Entry(ts, msg if isinstance(msg, str) else None)
