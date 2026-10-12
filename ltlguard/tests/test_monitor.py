"""Runtime tests. They use a precompiled fixture, so they run without Spot."""

import json
import shutil
from pathlib import Path

import pytest

from ltlguard.core.dfa import CompiledRulesError, load_compiled, parse_compiled
from ltlguard.core.monitor import Monitor, Verdict

DATA = Path(__file__).parent / "data"
YAML = DATA / "fixture_rules.yaml"
DFA = DATA / "fixture_rules.dfa.json"


@pytest.fixture
def ruleset():
    return load_compiled(DFA, YAML)


def run(monitor, *steps):
    """Commit steps; every step is a call to an allowed tool unless it says otherwise."""
    for s in steps:
        monitor.commit(frozenset(s) | {"allowed"})
    return monitor.verdicts()


def preview(monitor, call, possible=frozenset()):
    return monitor.preview(frozenset(call) | {"allowed"}, possible)


def test_initial_verdicts(ruleset):
    v = Monitor(ruleset).verdicts()
    assert v["A"] is Verdict.TENTATIVE_OK
    assert v["C"] is Verdict.TENTATIVE_VIOLATION  # F notified: not met on the empty trace
    assert v["G"] is Verdict.TENTATIVE_OK


def test_always(ruleset):
    m = Monitor(ruleset)
    assert run(m, set(), set())["A"] is Verdict.TENTATIVE_OK
    assert [r.id for r in m.preview(frozenset()).denied_by] == ["A"]
    m.commit(frozenset())
    assert m.verdicts()["A"] is Verdict.VIOLATED


def test_not_until_needs_both_checks(ruleset):
    m = Monitor(ruleset)
    assert not preview(m, {"restart"}).allowed
    run(m, {"status_checked"})
    assert [r.id for r in preview(m, {"restart"}).denied_by] == ["B"]
    run(m, {"logs_read"})
    assert preview(m, {"restart"}).allowed
    assert run(m, set())["B"] is Verdict.SATISFIED


def test_eventually(ruleset):
    m = Monitor(ruleset)
    assert run(m, set(), set())["C"] is Verdict.TENTATIVE_VIOLATION
    assert [r.id for r in m.pending_obligations()] == ["C"]
    assert run(m, {"notified"})["C"] is Verdict.SATISFIED
    assert m.pending_obligations() == []


def test_at_most_once(ruleset):
    m = Monitor(ruleset)
    assert preview(m, {"notify_info"}).allowed
    run(m, {"notify_info"})
    assert [r.id for r in preview(m, {"notify_info"}).denied_by] == ["D"]
    assert preview(m, set()).allowed


def test_response_is_obligation_not_denial(ruleset):
    m = Monitor(ruleset)
    run(m, {"status_checked"}, {"logs_read"}, {"restart"})
    assert m.verdicts()["E"] is Verdict.TENTATIVE_VIOLATION
    assert run(m, {"probe_ok"})["E"] is Verdict.TENTATIVE_OK


def test_result_props_are_existential_at_preview(ruleset):
    m = Monitor(ruleset)
    # cleanup together with a possible backup_ok in the same step would be fine for rule G...
    assert preview(m, {"cleanup"}, {"backup_ok"}).allowed
    # ...but a cleanup call cannot produce backup_ok, so it is denied.
    assert [r.id for r in preview(m, {"cleanup"}, set()).denied_by] == ["G"]
    run(m, {"backup_ok"})
    assert preview(m, {"cleanup"}).allowed


def test_log_only_rules_do_not_deny(ruleset):
    m = Monitor(ruleset)
    p = preview(m, {"bad"})
    assert p.allowed
    assert [r.id for r in p.shadow_denied_by] == ["F"]


def test_violated_rule_stops_gating(ruleset):
    m = Monitor(ruleset)
    m.commit(frozenset())  # violates A
    assert m.verdicts()["A"] is Verdict.VIOLATED
    assert m.preview(frozenset()).allowed


def test_preview_does_not_change_state(ruleset):
    m = Monitor(ruleset)
    before = dict(m.states)
    preview(m, {"notify_info", "restart"})
    assert m.states == before


def test_hash_mismatch(tmp_path, ruleset):
    y = tmp_path / "rules.yaml"
    y.write_text(YAML.read_text() + "\n# edited\n")
    with pytest.raises(CompiledRulesError, match="different"):
        load_compiled(DFA, y)


def test_malformed_files_are_rejected(tmp_path):
    data = json.loads(DFA.read_text())
    # Make a transition non-deterministic.
    data["rules"][0]["states"][0]["transitions"].append({"guard": "true", "to": 0})
    with pytest.raises(CompiledRulesError):
        parse_compiled(data)
    data = json.loads(DFA.read_text())
    data["rules"][0]["states"][0]["safe_next"] = "true"
    with pytest.raises(CompiledRulesError):
        parse_compiled(data)
    data = json.loads(DFA.read_text())
    data["format"] = "other"
    with pytest.raises(CompiledRulesError):
        parse_compiled(data)
    p = tmp_path / "x.json"
    p.write_text("{not json")
    with pytest.raises(CompiledRulesError):
        load_compiled(p)


def test_snapshot_restore(ruleset):
    m = Monitor(ruleset)
    run(m, {"notify_info"})
    m2 = Monitor(ruleset, m.snapshot()["states"])
    assert m2.states == m.states
