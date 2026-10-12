import base64
import json

from blossoml_ops.guard import check_output
from blossoml_ops.tools.logs import Entry, degrade, journal_entries, summarize

NOW = 1_800_000_000.0
SHA = "ab" * 32
PK = "cd" * 32


def entries():
    cur = NOW - 600       # in the current 1h window
    prev = NOW - 5400     # in the previous 1h window
    return [
        Entry(prev, "+Response: GET /x -> 200 [a: b]"),
        Entry(prev, "+Save failed: disk full"),
        Entry(cur, "+Request: GET /"),
        Entry(cur, "+Path parts: []"),
        Entry(cur, "+Response: GET / -> 200 [content-type: text/plain]"),
        Entry(cur, f"+Upload successful: {SHA} (1234 bytes, image/png)"),
        Entry(cur, "+Response: PUT /upload -> 201 [x: y]"),
        Entry(cur, "+Response: PUT /upload -> 500 [x: y]"),
        Entry(cur, "+Save failed: No space left on device"),
        Entry(cur, "+Save failed: No space left on device"),
        Entry(cur, f"+Delete request for {SHA} by {PK}"),
        Entry(cur, f"+WARNING: Failed to unlink file {SHA}: EACCES"),
        Entry(cur, "+Mirror failed: timeout"),
        Entry(cur, "+SSRF blocked: example.internal resolved to 10.0.0.1 (private)"),
        Entry(cur, f"+Report received: event={SHA} reporter={PK} entries=1"),
        Entry(cur, "+Response: PUT /report -> 200 [x: y]"),
        Entry(cur, "+Uncaught exception: Not_found"),
        Entry(cur, None),
        Entry(cur, "+Starting Blossom server on 0.0.0.0:8082 (base URL: https://x)"),
        Entry(cur, "+Save failed: " + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode()),
    ]


def test_errors_summary():
    out = summarize(entries(), "errors", 1, NOW)
    cur = out["current_window"]["error_counts"]
    assert cur["save_failed"] == 3
    assert cur["http_5xx"] == 1
    assert cur["unlink_failed"] == 1
    assert cur["mirror_failed"] == 1
    assert cur["ssrf_blocked"] == 1
    assert cur["other_error"] == 1
    assert out["previous_window"]["error_counts"] == {"save_failed": 1}
    assert out["undecodable_lines"] == 1
    # The sample containing an encoded image is dropped, not returned.
    assert out["samples_dropped"] == 1
    assert all("iVBOR" not in s["line"] for s in out["samples"])
    check_output(out)


def test_reports_are_ignored():
    out = summarize(entries(), "all_summary", 1, NOW)
    text = json.dumps(out)
    assert "report" not in text.lower()
    assert out["current_window"]["by_status"].get("200") == 1  # PUT /report response not counted


def test_uploads_and_status_codes():
    up = summarize(entries(), "uploads", 1, NOW)["current_window"]
    assert up["upload_ok"] == 1 and up["upload_bytes"] == 1234
    assert up["put_upload_status"] == {"201": 1, "500": 1}
    sc = summarize(entries(), "status_codes", 1, NOW)["current_window"]
    assert sc["by_status"] == {"200": 1, "201": 1, "500": 1}
    assert sc["by_method_class"]["PUT 5xx"] == 1


def test_deletes_samples_and_mask():
    out = summarize(entries(), "deletes", 1, NOW, mask_pubkeys=True)
    assert out["current_window"]["delete_request"] == 1
    assert out["current_window"]["unlink_failed"] == 1
    assert len(out["samples"]) == 1
    plain = summarize(entries(), "all_summary", 1, NOW)
    assert "samples" not in plain
    masked = summarize([Entry(NOW - 1, f"+Delete failed for {SHA} by {PK}: x")], "deletes", 1, NOW, mask_pubkeys=True)
    assert PK not in masked["samples"][0]["line"] and SHA in masked["samples"][0]["line"]


def test_split_at_deploy():
    out = summarize(entries() + [Entry(NOW - 10, "+Save failed: again")], "errors", 1, NOW, deploy_time=NOW - 60)
    split = out["split_at_last_deploy"]
    assert split["after"]["error_counts"] == {"save_failed": 1}
    assert split["before"]["error_counts"]["save_failed"] == 3


def test_degrade():
    out = degrade(summarize(entries(), "errors", 1, NOW))
    assert "samples" not in out and "degraded" in out


def test_journalctl_fallback_parsing():
    class FakeExec:
        def __init__(self):
            self.argv = None

        def stream_lines(self, argv, timeout, max_lines):
            self.argv = argv
            yield json.dumps({"MESSAGE": "+Request: GET /", "__REALTIME_TIMESTAMP": "1800000000000000"}).encode()
            yield json.dumps({"MESSAGE": [255, 254, 0], "__REALTIME_TIMESTAMP": "1800000001000000"}).encode()
            yield b"not json"

    ex = FakeExec()
    got = list(journal_entries("blossoml.service", 1_799_990_000, ex))
    assert got[0] == Entry(1_800_000_000.0, "+Request: GET /")
    assert got[1].message is None
    assert ex.argv[0] == "/usr/bin/journalctl"
    assert "_SYSTEMD_UNIT=blossoml.service" in ex.argv
    assert "--since=@1799990000" in ex.argv
