import json
from pathlib import Path

import pytest

from ltlguard.core.dfa import load_compiled
from ltlguard.core.labeling import Candidate
from ltlguard.core.monitor import Monitor
from ltlguard.core.session import GuardSession, NotCleared
from ltlguard.trace.jsonl import TraceWriter, read_events

DATA = Path(__file__).parent / "data"


class FakeLabeler:
    ALLOWED = {"status", "logs", "restart", "probe", "notify", "cleanup", "backup"}

    def call_props(self, tool, args):
        p = set()
        if tool in self.ALLOWED:
            p.add("allowed")
        if tool == "restart":
            p.add("restart")
        if tool == "cleanup":
            p.add("cleanup")
        if tool == "notify" and args.get("severity") == "info":
            p.add("notify_info")
        if tool == "boom":
            raise RuntimeError("labeler crashed")
        return frozenset(p)

    def possible_result_props(self, tool, args):
        return frozenset(
            {"status": {"status_checked"}, "logs": {"logs_read"}, "probe": {"probe_ok"}, "notify": {"notified"}, "backup": {"backup_ok"}}.get(tool, set())
        )

    def result_props(self, tool, args, result):
        if result is None:
            return frozenset()
        out = set()
        if tool == "status":
            out.add("status_checked")
        if tool == "logs":
            out.add("logs_read")
        if tool == "probe" and result.get("status") == 200:
            out.add("probe_ok")
        if tool == "notify" and result.get("accepted", 0) > 0:
            out.add("notified")
        if tool == "backup" and result.get("ok"):
            out.add("backup_ok")
        return frozenset(out)

    def all_props(self):
        return frozenset({"allowed", "restart", "cleanup", "notify_info", "status_checked", "logs_read", "probe_ok", "notified", "backup_ok"})

    def candidates(self):
        return [Candidate(t, t) for t in sorted(self.ALLOWED - {"notify"})] + [Candidate("notify(info)", "notify", {"severity": "info"})]


@pytest.fixture
def env(tmp_path):
    rs = load_compiled(DATA / "fixture_rules.dfa.json", DATA / "fixture_rules.yaml")
    alerts = []
    trace = TraceWriter(tmp_path / "traces", "s1")
    session = GuardSession(
        Monitor(rs), FakeLabeler(), trace,
        on_alert=lambda sev, title, body: alerts.append((sev, title)),
        state_path=str(tmp_path / "state.json"),
    )
    return session, alerts, tmp_path


def call(session, tool, args=None, result=None, ok=True, call_id=None):
    d = session.before_call(tool, args or {}, call_id)
    if not d.allow:
        return d
    session.require_cleared(tool, args or {})
    session.record_result(tool, result, ok)
    session.after_call(tool, call_id)
    return d


def events(tmp_path):
    return list(read_events(sorted((tmp_path / "traces").glob("*.jsonl"))))


def test_allow_and_commit(env):
    session, alerts, tmp = env
    assert call(session, "status", result={"active": True}).allow
    assert session.monitor.verdicts()["B"].value == "tentative_ok"
    kinds = [e["event"] for e in events(tmp)]
    assert kinds == ["pre", "post"]
    state = json.loads((tmp / "state.json").read_text())
    assert state["session"] == "s1"


def test_deny_message_lists_rule_and_allowed_ops(env):
    session, alerts, tmp = env
    d = call(session, "restart")
    assert not d.allow
    assert d.rules == ["B"]
    assert "ルール B「check before restart」に違反するため実行していません。" in d.message
    assert "理由: call status and logs before restart" in d.message
    assert "現在実行できる操作: " in d.message
    ops = d.message.rsplit("現在実行できる操作: ", 1)[1].split(", ")
    assert "restart" not in ops and "cleanup" not in ops and "status" in ops
    assert events(tmp)[-1]["event"] == "deny"


def test_unknown_tool_is_denied_and_alerts(env):
    session, alerts, tmp = env
    d = call(session, "Bash", {"command": "ls"})
    assert not d.allow and d.rules == ["A"]
    assert alerts and alerts[0][0] == "critical"


def test_result_props_from_tool_result(env):
    session, alerts, tmp = env
    call(session, "probe", result={"status": 500})
    assert "probe_ok" not in events(tmp)[-1]["props"]
    call(session, "probe", result={"status": 200})
    assert "probe_ok" in events(tmp)[-1]["props"]


def test_failed_tool_commits_only_call_props(env):
    session, alerts, tmp = env
    session.before_call("notify", {"severity": "info"})
    session.require_cleared("notify", {"severity": "info"})
    session.after_call("notify", None, failed=True)
    ev = events(tmp)[-1]
    assert ev["props"] == ["allowed", "notify_info"] and ev["ok"] is False
    # A second info notification is denied even though the first failed.
    assert not session.before_call("notify", {"severity": "info"}).allow


def test_concurrent_calls_are_serialized(env):
    session, alerts, tmp = env
    assert session.before_call("status", {}, "c1").allow
    d = session.before_call("logs", {}, "c2")
    assert not d.allow and "1 つずつ" in d.message
    session.after_call("status", "c1")
    assert session.before_call("logs", {}, "c3").allow


def test_stale_inflight_is_committed(env):
    session, alerts, tmp = env
    now = [0.0]
    session.clock = lambda: now[0]
    assert session.before_call("notify", {"severity": "info"}).allow
    now[0] = 10_000.0
    d = session.before_call("notify", {"severity": "info"})
    assert not d.allow and d.rules == ["D"]


def test_require_cleared(env):
    session, alerts, tmp = env
    with pytest.raises(NotCleared):
        session.require_cleared("status", {})
    session.before_call("status", {"x": 1})
    with pytest.raises(NotCleared):
        session.require_cleared("status", {"x": 2})
    session.require_cleared("status", {"x": 1})
    with pytest.raises(NotCleared):  # only once per cleared call
        session.require_cleared("status", {"x": 1})
    assert alerts


def test_internal_error_fails_closed(env):
    session, alerts, tmp = env
    d = session.before_call("boom", {})
    assert not d.allow and "fail-closed" in d.message


def test_broken_monitor_denies_everything(env):
    session, alerts, tmp = env
    session.before_call("status", {})

    def explode(props):
        raise RuntimeError("x")

    session.monitor.commit = explode
    session.after_call("status", None)
    assert session.broken
    assert not session.before_call("status", {}).allow
    assert any(a[0] == "critical" for a in alerts)


def test_stop_reprompts_then_gives_up(env):
    session, alerts, tmp = env
    msg = session.at_stop()
    assert msg and "C" in msg
    assert session.at_stop()
    assert session.at_stop() is None
    assert session.unmet_at_stop == ["C"]


def test_stop_ok_after_obligation(env):
    session, alerts, tmp = env
    call(session, "notify", {"severity": "info"}, result={"accepted": 2})
    assert session.at_stop() is None
    assert session.unmet_at_stop == []


def test_shadow_rule_is_traced(env):
    session, alerts, tmp = env

    class L(FakeLabeler):
        def call_props(self, tool, args):
            return super().call_props(tool, args) | {"bad"}

    session.labeler = L()
    assert session.before_call("status", {}).allow
    assert any(e["event"] == "shadow_deny" for e in events(tmp))
