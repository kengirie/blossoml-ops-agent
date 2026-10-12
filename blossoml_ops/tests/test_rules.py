"""The real rules.yaml / rules.dfa.json wired to the real labeler."""

import pytest

from ltlguard.core.dfa import load_compiled
from ltlguard.core.monitor import Monitor, Verdict

from blossoml_ops.agent import RULES_DFA, RULES_YAML, check_rules
from blossoml_ops.labeling import OpsLabeler, full_name
from blossoml_ops.tools import build_specs, representative_args


@pytest.fixture(params=[False, True], ids=["phase1", "phase2"])
def setup(request):
    specs = build_specs(request.param)
    labeler = OpsLabeler([s.name for s in specs], representative_args(specs))
    return Monitor(load_compiled(RULES_DFA, RULES_YAML)), labeler


def call(monitor, labeler, short, args=None, result=None):
    """Preview, then commit like the session would. Returns the denying rule ids."""
    tool = short if short.startswith(("mcp__", "Bash", "Read", "WebFetch")) else full_name(short)
    args = args or {}
    p = monitor.preview(labeler.call_props(tool, args), labeler.possible_result_props(tool, args))
    if not p.allowed:
        return [r.id for r in p.denied_by]
    monitor.commit(labeler.call_props(tool, args) | labeler.result_props(tool, args, result))
    return []


def test_rules_consistent_with_labeler(setup):
    monitor, labeler = setup
    assert check_rules(monitor.ruleset, labeler) == []
    assert "R3" not in [r.id for r in monitor.rules]  # approval is out of scope


@pytest.mark.parametrize("tool", ["Bash", "Read", "WebFetch", "mcp__other__x", "mcp__blossoml__delete_blob"])
def test_r0_denies_unregistered_tools(setup, tool):
    monitor, labeler = setup
    assert call(monitor, labeler, tool, {}) == ["R0"]


def test_r0_denies_write_tools_in_phase1():
    specs = build_specs(False)
    labeler = OpsLabeler([s.name for s in specs])
    monitor = Monitor(load_compiled(RULES_DFA, RULES_YAML))
    assert call(monitor, labeler, "restart_service") == ["R0"]


@pytest.mark.parametrize("args", [{"target": "blob_get"}, {"target": "root_get", "path": "/x"}, {}])
def test_r6_probe_allowlist(setup, args):
    monitor, labeler = setup
    assert call(monitor, labeler, "http_probe", args) == ["R6"]
    assert call(monitor, labeler, "http_probe", {"target": "root_get"}, {"status": 200}) == []


def test_r5_prime_report_obligation(setup):
    monitor, labeler = setup
    assert [r.id for r in monitor.pending_obligations()] == ["R5'"]
    call(monitor, labeler, "notify", {"severity": "info", "title": "t", "body": "b"}, {"accepted_relays": 0})
    assert [r.id for r in monitor.pending_obligations()] == ["R5'"]  # not accepted by any relay
    call(monitor, labeler, "notify", {"severity": "warning", "title": "t", "body": "b"}, {"accepted_relays": 2})
    assert monitor.pending_obligations() == []
    assert monitor.verdicts()["R5'"] is Verdict.SATISFIED


def test_r8_one_info(setup):
    monitor, labeler = setup
    args = {"severity": "info", "title": "t", "body": "b"}
    assert call(monitor, labeler, "notify", args, {"accepted_relays": 1}) == []
    assert call(monitor, labeler, "notify", args) == ["R8"]


def test_r9_five_alerts(setup):
    monitor, labeler = setup
    for i in range(5):
        sev = "warning" if i % 2 else "critical"
        assert call(monitor, labeler, "notify", {"severity": sev, "title": "t", "body": "b"}, {"accepted_relays": 1}) == []
    assert call(monitor, labeler, "notify", {"severity": "warning", "title": "t", "body": "b"}) == ["R9"]
    assert call(monitor, labeler, "notify", {"severity": "info", "title": "t", "body": "b"}, {"accepted_relays": 1}) == []


def phase2():
    specs = build_specs(True)
    return Monitor(load_compiled(RULES_DFA, RULES_YAML)), OpsLabeler([s.name for s in specs], representative_args(specs))


def test_r1_checks_before_restart():
    monitor, labeler = phase2()
    assert call(monitor, labeler, "restart_service") == ["R1"]
    call(monitor, labeler, "service_status", {}, {"active_state": "failed"})
    assert call(monitor, labeler, "restart_service") == ["R1"]
    call(monitor, labeler, "read_logs", {"since_hours": 1, "kind": "errors"}, {"kind": "errors"})
    assert call(monitor, labeler, "restart_service", {}, {"ok": True}) == []


def test_r1_failed_check_does_not_count():
    monitor, labeler = phase2()
    call(monitor, labeler, "service_status", {}, {"error": "tool_failed"})
    call(monitor, labeler, "read_logs", {"since_hours": 1, "kind": "errors"}, {"kind": "errors"})
    assert call(monitor, labeler, "restart_service") == ["R1"]


def test_r2_and_r4_restart():
    monitor, labeler = phase2()
    call(monitor, labeler, "service_status", {}, {})
    call(monitor, labeler, "read_logs", {"since_hours": 1, "kind": "errors"}, {})
    assert call(monitor, labeler, "restart_service", {}, {"ok": True}) == []
    assert monitor.verdicts()["R4"] is Verdict.TENTATIVE_VIOLATION
    assert "R4" in [r.id for r in monitor.pending_obligations()]
    assert call(monitor, labeler, "restart_service") == ["R2"]
    call(monitor, labeler, "http_probe", {"target": "upload_head"}, {"status": 401})
    assert monitor.verdicts()["R4"] is Verdict.TENTATIVE_VIOLATION
    call(monitor, labeler, "http_probe", {"target": "root_get"}, {"status": 503})
    assert monitor.verdicts()["R4"] is Verdict.TENTATIVE_VIOLATION
    call(monitor, labeler, "http_probe", {"target": "root_get"}, {"status": 200})
    assert monitor.verdicts()["R4"] is Verdict.TENTATIVE_OK


def test_r5_backup_before_cleanup():
    monitor, labeler = phase2()
    assert call(monitor, labeler, "cleanup_tmp", {"min_age_hours": 24}) == ["R5"]
    call(monitor, labeler, "backup_db", {}, {"ok": False, "exit_code": 1})
    assert call(monitor, labeler, "cleanup_tmp", {"min_age_hours": 24}) == ["R5"]
    call(monitor, labeler, "backup_db", {}, {"ok": True, "exit_code": 0})
    assert call(monitor, labeler, "cleanup_tmp", {"min_age_hours": 24}, {"ok": True}) == []


def test_allowed_operations_listing():
    from ltlguard.core.session import GuardSession

    monitor, labeler = phase2()

    class T:
        session = "t"

        def write(self, *a, **k):
            return {}

    s = GuardSession(monitor, labeler, T())
    d = s.before_call(full_name("restart_service"), {})
    assert not d.allow
    ops = d.message.rsplit("現在実行できる操作: ", 1)[1].split(", ")
    assert "service_status" in ops and "notify(severity=info)" in ops
    assert "restart_service" not in ops and "cleanup_tmp" not in ops
