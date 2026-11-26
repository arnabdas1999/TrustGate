import threading
from datetime import UTC, datetime, timedelta

import pytest

from contracts.hashing import payload_hash, sign_approval
from contracts.models import Approval, WriteOp
from gate.approvals import ApprovalRefused, ApprovalStore, Refusal
from gate.audit_log import AuditLog
from gate.executor import Executor, StaleState, WriteFailed
from gate.mcp_client import McpError, ToolResult
from gate.scope import GateScope
from gate.writer import V2_WRITE_TOOL, StaticIds

SECRET = "exec-secret"
NOW = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
SCOPE = GateScope(project_id=56664, project_name="OpsPilot", environments={"Development": 121197})
IDS = StaticIds(features={"agent_prompt_version": 306127, "agent_tool_policy": 306128},
                segments={"tier0_production": 1205876})
FP_OK, FP_NEW = "a" * 64, "b" * 64
OK = ToolResult(False, [{"type": "text", "text": '{"uuid": "v-1", "published": true}'}])


def _ops(feature="agent_prompt_version", value="v1_strict"):
    return [WriteOp(tool="create_segment_override", project="OpsPilot", environment="Development",
                    feature=feature, segment="tier0_production", value=value)]


def _sign(ops, approval_id="appr_1", proposal_id="prop_1", secret=SECRET):
    exp = NOW + timedelta(minutes=10)
    ph = payload_hash(ops)
    a = Approval(proposal_id=proposal_id, approval_id=approval_id, approver="naman",
                 payload_hash=ph, created_at=NOW, expires_at=exp,
                 signature=sign_approval(secret, approval_id, proposal_id, ph, exp.isoformat()))
    return Approval.model_validate(a.model_dump(mode="json"))


class FakeMcp:
    def __init__(self, results=None, exc=None, session_error=None):
        self.results = list(results or [OK, OK, OK])
        self.exc, self.session_error = exc, session_error
        self.sent, self.session_checks = [], 0
        self.lock = threading.Lock()

    def ensure_session(self):
        self.session_checks += 1
        if self.session_error:
            raise self.session_error

    def call_tool(self, name, arguments, *, is_write):
        with self.lock:
            self.sent.append((name, arguments, is_write))
        if self.exc:
            raise self.exc
        return self.results.pop(0)


class Fingerprints:
    def __init__(self, *values, default=None):
        self.values = list(values)
        self.default = default
        self.asked = []

    def __call__(self, environment):
        self.asked.append(environment)
        value = self.values.pop(0) if self.values else self.default
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def parts(tmp_path):
    approvals = ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE, clock=lambda: NOW)
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: NOW)
    return approvals, audit


def _approve(parts, ops, *, approval_id="appr_1", proposal_id="prop_1", fp=FP_OK,
             secret=SECRET, register=True):
    if register:
        parts[0].register_proposal(proposal_id, payload_hash(ops), fp, "Development")
    return _sign(ops, approval_id, proposal_id, secret)


def _executor(parts, mcp, fps, settle_reads=1, ids=IDS):
    approvals, audit = parts
    return Executor(approvals=approvals, scope=SCOPE, ids=ids, mcp=mcp, fingerprint=fps,
                    audit=audit, settle_reads=settle_reads, sleep=lambda s: None)


def test_approved_write_is_sent_and_audited(parts):
    mcp, fps = FakeMcp(), Fingerprints(FP_OK, FP_NEW)
    ops = _ops()
    result = _executor(parts, mcp, fps).execute(_approve(parts, ops), ops)
    assert result.status == "applied"
    assert result.fingerprint_before == FP_OK and result.fingerprint_after == FP_NEW
    assert result.version_uuids == ["v-1"]
    assert [(s[0], s[2]) for s in mcp.sent] == [(V2_WRITE_TOOL, True)]
    assert mcp.sent[0][1] == result.calls[0].arguments
    entry = parts[1].entries()[-1]
    assert entry["action"] == "write" and entry["approver_claimed"] == "naman"
    assert entry["approver_verified"] is False
    assert entry["payload_hash"] == payload_hash(ops)
    assert entry["fingerprint_before"] == FP_OK and entry["fingerprint_after"] == FP_NEW
    assert parts[0].is_used("appr_1")


def test_stale_check_uses_the_gates_registration_not_the_request(parts):
    # The caller can't skip the stale check by sending the current fingerprint.
    mcp = FakeMcp()
    ops = _ops()
    approval = _approve(parts, ops, fp=FP_OK)
    # Sending the current fingerprint instead of the registered one is refused outright...
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_NEW)).execute(approval, ops, FP_NEW)
    assert err.value.reason is Refusal.BAD_REQUEST
    # ...and without one, the gate still compares against its own registration.
    with pytest.raises(StaleState):
        _executor(parts, mcp, Fingerprints(FP_NEW)).execute(approval, ops)
    assert mcp.sent == []


def test_unregistered_proposal_is_refused(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(
            _approve(parts, ops, register=False), ops)
    assert err.value.reason is Refusal.BAD_REQUEST and mcp.sent == []


def test_registration_for_a_different_payload_is_refused(parts):
    mcp = FakeMcp()
    registered, sent = _ops(value="v1_strict"), _ops(value="v1_strict")
    parts[0].register_proposal("prop_1", payload_hash(_ops(value="other")), FP_OK, "Development")
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(
            _approve(parts, registered, register=False), sent)
    assert err.value.reason is Refusal.PAYLOAD_MISMATCH and mcp.sent == []


def test_request_fingerprint_that_disagrees_with_the_registration_is_refused(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approve(parts, ops), ops, FP_NEW)
    assert err.value.reason is Refusal.BAD_REQUEST and mcp.sent == []


def test_concurrent_approvals_on_one_fingerprint_write_once(parts):
    # Two proposals from the same state, approved at the same time. The edge lags, so the
    # second still sees the old fingerprint: the written-over record must stop it.
    mcp = FakeMcp()
    fps = Fingerprints(default=FP_OK)  # lagging edge: never shows the write
    ex = _executor(parts, mcp, fps, settle_reads=2)
    a = _approve(parts, _ops(), approval_id="appr_a", proposal_id="prop_a")
    b_ops = _ops(feature="agent_tool_policy", value="restricted")
    b = _approve(parts, b_ops, approval_id="appr_b", proposal_id="prop_b")
    outcomes, barrier = [], threading.Barrier(2)

    def run(approval, ops):
        barrier.wait()
        try:
            ex.execute(approval, ops)
            outcomes.append("applied")
        except StaleState:
            outcomes.append("stale")

    threads = [threading.Thread(target=run, args=(a, _ops())),
               threading.Thread(target=run, args=(b, b_ops))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(outcomes) == ["applied", "stale"]
    assert len(mcp.sent) == 1


def test_forged_approval_sends_nothing(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approve(parts, ops, secret="x"), ops)
    assert err.value.reason is Refusal.FORGED
    assert mcp.sent == []
    assert parts[1].entries()[-1]["action"] == "refused"


def test_stale_state_sends_nothing_and_keeps_the_approval(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(StaleState):
        _executor(parts, mcp, Fingerprints(FP_NEW)).execute(_approve(parts, ops), ops)
    assert mcp.sent == [] and not parts[0].is_used("appr_1")
    assert "stale" in parts[1].entries()[-1]["result"]


def test_reused_approval_writes_once(parts):
    mcp = FakeMcp()
    ops = _ops()
    approval = _approve(parts, ops)
    ex = _executor(parts, mcp, Fingerprints(FP_OK, FP_NEW, FP_NEW))
    ex.execute(approval, ops)
    with pytest.raises((ApprovalRefused, StaleState)):
        ex.execute(approval, ops)
    assert len(mcp.sent) == 1


def test_dead_mcp_session_fails_before_the_approval_is_burned(parts):
    mcp = FakeMcp(session_error=McpError("MCP session could not be re-established"))
    ops = _ops()
    with pytest.raises(WriteFailed):
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approve(parts, ops), ops)
    assert mcp.sent == [] and not parts[0].is_used("appr_1")
    assert "session" in parts[1].entries()[-1]["result"]


def test_tool_error_is_a_failed_write_and_burns_the_approval(parts):
    mcp = FakeMcp(results=[ToolResult(True, [{"type": "text", "text": "HTTP error 400"}])])
    ops = _ops()
    with pytest.raises(WriteFailed) as err:
        _executor(parts, mcp, Fingerprints(FP_OK, FP_OK)).execute(_approve(parts, ops), ops)
    assert err.value.result.status == "failed"
    assert parts[0].is_used("appr_1")
    assert parts[1].entries()[-1]["result"].startswith("failed")


def test_network_error_on_write_is_not_retried(parts):
    mcp = FakeMcp(exc=McpError("network error during write (not retried)"))
    ops = _ops()
    with pytest.raises(WriteFailed):
        _executor(parts, mcp, Fingerprints(FP_OK, FP_OK)).execute(_approve(parts, ops), ops)
    assert len(mcp.sent) == 1


def test_partial_write_still_records_the_after_fingerprint(parts):
    bad = ToolResult(True, [{"type": "text", "text": "HTTP error 400"}])
    mcp = FakeMcp(results=[OK, bad])
    ops = [*_ops(), *_ops(feature="agent_tool_policy", value="restricted")]
    with pytest.raises(WriteFailed) as err:
        _executor(parts, mcp, Fingerprints(FP_OK, FP_NEW)).execute(_approve(parts, ops), ops)
    assert err.value.result.fingerprint_after == FP_NEW
    assert parts[0].was_written_over("Development", FP_OK)


def test_unrenderable_ops_send_nothing_and_keep_the_approval(parts):
    mcp = FakeMcp()
    ops = _ops(feature="not_a_flag")
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approve(parts, ops), ops)
    assert err.value.reason is Refusal.BAD_REQUEST
    assert mcp.sent == [] and not parts[0].is_used("appr_1")


def test_resolver_failures_are_audited_refusals_not_500s(parts):
    class BrokenIds:
        def feature_id(self, name):
            raise McpError("list_project_features failed")

        def segment_id(self, name):
            raise KeyError("id")

    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK), ids=BrokenIds()).execute(
            _approve(parts, ops), ops)
    assert err.value.reason is Refusal.BAD_REQUEST
    assert parts[1].entries()[-1]["action"] == "refused" and mcp.sent == []


def test_fingerprint_unavailable_means_no_write(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(StaleState, match="could not"):
        _executor(parts, mcp, Fingerprints(RuntimeError("edge down"))).execute(
            _approve(parts, ops), ops)
    assert mcp.sent == []


def test_after_fingerprint_failure_does_not_hide_a_successful_write(parts):
    mcp = FakeMcp()
    ops = _ops()
    result = _executor(parts, mcp, Fingerprints(FP_OK, RuntimeError("edge down"))).execute(
        _approve(parts, ops), ops)
    assert result.status == "applied" and result.fingerprint_after is None


def test_after_fingerprint_waits_for_the_edge_to_catch_up(parts):
    mcp, fps = FakeMcp(), Fingerprints(FP_OK, FP_OK, FP_OK, FP_NEW)
    ops = _ops()
    result = _executor(parts, mcp, fps, settle_reads=5).execute(_approve(parts, ops), ops)
    assert result.fingerprint_after == FP_NEW and result.after_settled


def test_after_fingerprint_gives_up_and_says_so(parts):
    mcp, fps = FakeMcp(), Fingerprints(FP_OK, FP_OK, FP_OK, FP_OK)
    ops = _ops()
    result = _executor(parts, mcp, fps, settle_reads=3).execute(_approve(parts, ops), ops)
    assert result.status == "applied"
    assert result.fingerprint_after == FP_OK and not result.after_settled
    assert parts[1].entries()[-1]["after_settled"] is False


def test_default_settle_budget_fits_inside_the_apis_30s_timeout(parts):
    ex = Executor(approvals=parts[0], scope=SCOPE, ids=IDS, mcp=FakeMcp(),
                  fingerprint=Fingerprints(), audit=parts[1])
    assert ex._settle_reads * ex._settle_interval_s <= 10


# --- second review --------------------------------------------------------------------------


def test_ambiguous_write_still_blocks_the_old_fingerprint(parts):
    # The write timed out: it may have landed. The state it started from must be refused.
    mcp = FakeMcp(exc=McpError("network error during write (not retried)"))
    ex = _executor(parts, mcp, Fingerprints(default=FP_OK), settle_reads=2)
    with pytest.raises(WriteFailed):
        ex.execute(_approve(parts, _ops(), approval_id="appr_a", proposal_id="prop_a"), _ops())
    assert parts[0].was_written_over("Development", FP_OK)
    b_ops = _ops(feature="agent_tool_policy", value="restricted")
    with pytest.raises(StaleState):
        ex.execute(_approve(parts, b_ops, approval_id="appr_b", proposal_id="prop_b"), b_ops)
    assert len(mcp.sent) == 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_settle_stops_at_its_time_budget_even_with_slow_reads(parts):
    clock = Clock()

    def slow_fp(env):
        clock.t += 3.0  # each edge read takes 3 s
        return FP_OK

    ex = Executor(approvals=parts[0], scope=SCOPE, ids=IDS, mcp=FakeMcp(), fingerprint=slow_fp,
                  audit=parts[1], settle_reads=50, settle_interval_s=1.0, sleep=clock.sleep,
                  monotonic=clock.now, settle_budget_s=10.0)
    ops = _ops()
    result = ex.execute(_approve(parts, ops), ops)
    assert result.status == "applied" and not result.after_settled
    assert clock.t <= 3.0 + 10.0 + 3.0  # before-read + budget + at most one read in flight


def test_session_check_crash_is_an_audited_failure_not_a_500(parts):
    mcp = FakeMcp(session_error=ValueError("ping reply was not JSON"))
    ops = _ops()
    with pytest.raises(WriteFailed):
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approve(parts, ops), ops)
    assert mcp.sent == [] and not parts[0].is_used("appr_1")
    assert "session" in parts[1].entries()[-1]["result"]


def test_consume_crash_is_an_audited_refusal(parts, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(parts[0], "consume", broken)
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(ApprovalRefused):
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approve(parts, ops), ops)
    assert mcp.sent == [] and parts[1].entries()[-1]["action"] == "refused"


def test_applied_write_is_audited_even_if_marking_written_over_fails(parts, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(parts[0], "mark_written_over", broken)
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(RuntimeError):
        _executor(parts, mcp, Fingerprints(FP_OK, FP_NEW)).execute(_approve(parts, ops), ops)
    entry = parts[1].entries()[-1]
    assert entry["action"] == "write" and entry["result"] == "applied"
