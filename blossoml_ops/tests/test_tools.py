import http.server
import json
import threading

import pytest

from ltlguard.core.automaton import load_rules
from ltlguard.core.monitor import Monitor
from ltlguard.core.session import GuardSession
from ltlguard.trace.jsonl import TraceWriter, read_events

from blossoml_ops.agent import RULES_YAML
from blossoml_ops.guard import check_output
from blossoml_ops.labeling import OpsLabeler, full_name
from blossoml_ops.tools import MetricsHistory, ToolContext, build_specs, db, probe, storage, system, write_ops
from blossoml_ops.tools.base import ToolRunner, ToolSpec
from blossoml_ops.tools.executor import CommandResult

from conftest import h


class FakeExecutor:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def run(self, argv, timeout=30.0):
        self.calls.append(list(argv))
        return self.results.get(argv[0], CommandResult(0, "", ""))

    def stream_lines(self, argv, timeout=120.0, max_lines=0):
        self.calls.append(list(argv))
        return iter(())


@pytest.fixture
def ctx(config, tmp_path):
    def make(phase2=False, executor=None, **cfg):
        c = config(**cfg)
        specs = build_specs(phase2)
        labeler = OpsLabeler([s.name for s in specs])
        trace = TraceWriter(tmp_path / "traces", "test")
        alerts = []
        session = GuardSession(Monitor(load_rules(RULES_YAML)), labeler, trace, on_alert=lambda *a: alerts.append(a))
        context = ToolContext(
            config=c, session=session, trace=trace, notifier=None,
            history=MetricsHistory(tmp_path / "state" / "metrics.json"),
            alert=lambda *a: alerts.append(a), executor=executor or FakeExecutor(),
        )
        context.alerts = alerts
        return context

    return make


def spec(name, phase2=False):
    return next(s for s in build_specs(phase2) if s.name == name)


async def invoke(context, name, args, phase2=False):
    s = spec(name, phase2)
    d = context.session.before_call(full_name(name), args)
    assert d.allow, d.message
    out = await ToolRunner(context).invoke(s, args)
    context.session.after_call(full_name(name), None)
    return json.loads(out["content"][0]["text"]), out.get("is_error", False)


# ---- individual tools ---------------------------------------------------------

def test_storage_audit(ctx):
    out = storage.storage_audit(ctx())
    assert out["blob_files"] == 4
    assert out["tmp_files_count"] == 2
    assert out["tmp_files"][0]["name"] == "tmp_1_2" and out["tmp_files"][0]["age_hours"] >= 29
    assert out["nonconforming_names"] == 1
    assert out["world_readable_files"] >= 1
    assert "weird.name" not in json.dumps(out)
    check_output(out)


def test_disk_usage_and_history(ctx, tmp_path):
    c = ctx()
    out = storage.disk_usage(c)
    assert out["data_dir_files"] == 7 and "previous_run" not in out
    c.history.save("run1")
    c2 = ctx()
    out2 = storage.disk_usage(c2)
    assert out2["previous_run"]["data_dir_files"] == 7


def test_deploy_info(ctx):
    import hashlib

    out = storage.deploy_info(ctx())
    assert out["main_exe"]["sha256"] == hashlib.sha256(b"binary-v2").hexdigest()
    assert out["main_exe_backup"]["exists"]


def test_db_reports(ctx):
    c = ctx()
    counts = db.db_report(c, "blob_counts_by_status")["by_status"]
    assert counts["stored"]["count"] == 3 and counts["deleted"]["count"] == 1
    recent = db.db_report(c, "recent_uploads_count")
    assert recent["last_24h"]["count"] == 3 and recent["previous_24h"]["count"] == 1
    owners = db.db_report(c, "owner_counts")
    assert owners["distinct_owners"] == 2 and owners["stored_blobs_without_owner"] == 1
    cons = db.db_report(c, "consistency")
    assert cons["stored_but_file_missing"] == {"count": 1, "sha256": [h(4)]}
    assert cons["file_without_db_row"] == {"count": 1, "sha256": [h(99)]}
    assert cons["file_present_but_status_not_stored"]["sha256"] == [h(3)]
    for name in db.REPORTS:
        check_output(db.db_report(c, name))


def test_db_is_read_only(ctx):
    import sqlite3

    conn = db._connect(ctx().config.db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM blobs")
    conn.close()


def test_service_status_parsing(ctx):
    ex = FakeExecutor({"/usr/bin/systemctl": CommandResult(0, "ActiveState=active\nSubState=running\nNRestarts=2\nMemoryCurrent=12345\nMainPID=42\nActiveEnterTimestamp=Sun 2026-10-11 10:00:00 UTC\nOther=x\n", "")})
    c = ctx(executor=ex)
    out = system.service_status(c)
    assert out["active_state"] == "active" and out["n_restarts"] == 2 and out["memory_current_bytes"] == 12345
    assert ex.calls[0][:3] == ["/usr/bin/systemctl", "show", "blossoml.service"]


def test_write_ops_use_fixed_sudo_argv(ctx):
    ex = FakeExecutor()
    c = ctx(executor=ex)
    write_ops.restart_service(c)
    write_ops.backup_db(c)
    write_ops.cleanup_tmp(c, 24)
    assert ex.calls == [
        ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "blossoml"],
        ["/usr/bin/sudo", "-n", "/usr/local/libexec/blossoml-ops/backup-db"],
        ["/usr/bin/sudo", "-n", "/usr/local/libexec/blossoml-ops/cleanup-tmp", "24"],
    ]


# ---- http_probe against a local server ------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    blob = b"hello test blob"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            body = "Terms of Service\nBe nice.\x00".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/" + Handler.sha:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(Handler.blob)
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path == "/upload":
            self.send_response(401)
            self.send_header("X-Reason", "Missing Authorization header")
            self.end_headers()
        else:
            self.send_response(200 if self.path == "/" + Handler.sha else 404)
            self.send_header("Content-Length", str(len(Handler.blob)))
            self.end_headers()


@pytest.fixture
def local_server():
    import hashlib

    Handler.sha = hashlib.sha256(Handler.blob).hexdigest()
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], Handler.sha
    srv.shutdown()


def test_http_probe(ctx, local_server):
    port, sha = local_server
    c = ctx(blossoml={"dir": str(ctx().config.blossoml_dir), "url": f"http://127.0.0.1:{port}"}, test_blob_sha256=sha)
    root = probe.http_probe(c, "root_get")
    assert root["status"] == 200 and root["body_head"].startswith("Terms of Service") and "\x00" not in root["body_head"]
    up = probe.http_probe(c, "upload_head")
    assert up["status"] == 401 and up["headers"]["x-reason"] == "Missing Authorization header"
    head = probe.http_probe(c, "test_blob_head")
    assert head["status"] == 200
    get = probe.http_probe(c, "test_blob_get")
    assert get["sha256_match"] is True and get["body_bytes"] == len(Handler.blob)
    assert "hello" not in json.dumps(get)  # never the body
    for r in (root, up, head, get):
        check_output(r)


def test_http_probe_wrong_test_blob(ctx, local_server):
    port, sha = local_server
    c = ctx(blossoml={"dir": str(ctx().config.blossoml_dir), "url": f"http://127.0.0.1:{port}"}, test_blob_sha256="0" * 64)
    assert probe.http_probe(c, "test_blob_get")["sha256_match"] is False


def test_http_probe_unreachable(ctx):
    c = ctx(blossoml={"dir": str(ctx().config.blossoml_dir), "url": "http://127.0.0.1:9"})
    out = probe.http_probe(c, "root_get")
    assert out["reachable"] is False


def test_probe_url_must_be_local(config):
    from blossoml_ops.config import ConfigError

    with pytest.raises(ConfigError):
        config(blossoml={"url": "http://example.com:8082"})


# ---- the runner: clearance, validation, output guard ------------------------------

async def test_runner_happy_path(ctx):
    c = ctx()
    out, err = await invoke(c, "storage_audit", {})
    assert not err and out["blob_files"] == 4
    ev = [e for e in read_events(sorted(c.trace.directory.glob("*.jsonl"))) if e["event"] == "post"][-1]
    assert ev["ok"] and ev["result_summary"]["blob_files"] == 4


async def test_runner_refuses_uncleared_call(ctx):
    c = ctx()
    out = await ToolRunner(c).invoke(spec("storage_audit"), {})
    assert json.loads(out["content"][0]["text"])["error"] == "not_cleared"
    assert c.alerts and c.alerts[0][0] == "critical"


async def test_runner_validates_arguments(ctx):
    c = ctx()
    for bad in ({"since_hours": 0, "kind": "errors"}, {"since_hours": 73, "kind": "errors"},
                {"since_hours": 1, "kind": "rm -rf"}, {"since_hours": True, "kind": "errors"},
                {"since_hours": 1, "kind": "errors", "path": "/etc"}):
        out, err = await invoke(c, "read_logs", bad)
        assert err and out["error"] == "invalid_arguments", bad


async def test_runner_discards_bad_output(ctx):
    c = ctx()

    def leaky(context):
        return {"data": "\x89PNG\r\n\x1a\n...."}

    s = ToolSpec(name="storage_audit", description="", params={}, run=leaky)
    c.session.before_call(full_name("storage_audit"), {})
    out = await ToolRunner(c).invoke(s, {})
    c.session.after_call(full_name("storage_audit"), None)
    payload = json.loads(out["content"][0]["text"])
    assert out["is_error"] and payload == {"error": "output_discarded", "reason_kind": "control_char"}
    assert "PNG" not in out["content"][0]["text"]
    assert c.alerts[-1][0] == "critical"
    events = list(read_events(sorted(c.trace.directory.glob("*.jsonl"))))
    gv = [e for e in events if e["event"] == "guard_violation"]
    assert gv and "PNG" not in json.dumps(events)


async def test_runner_hides_exception_text(ctx):
    c = ctx()

    def boom(context):
        raise RuntimeError("secret-ish detail /etc/blossoml-ops/anthropic_api_key")

    s = ToolSpec(name="disk_usage", description="", params={}, run=boom)
    c.session.before_call(full_name("disk_usage"), {})
    out = await ToolRunner(c).invoke(s, {})
    text = out["content"][0]["text"]
    assert out["is_error"] and "RuntimeError" in text and "anthropic" not in text


async def test_notify_arguments_are_guarded(ctx):
    import base64

    c = ctx()
    body = "see: " + base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20).decode()
    out, err = await invoke(c, "notify", {"severity": "info", "title": "t", "body": body})
    assert err and out["error"] == "invalid_arguments"
