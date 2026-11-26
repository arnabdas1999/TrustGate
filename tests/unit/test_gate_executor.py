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
IDS = StaticIds(features={"agent_prompt_version": 306127}, segments={"tier0_production": 1205876})
FP_OK, FP_NEW = "a" * 64, "b" * 64
OK = ToolResult(False, [{"type": "text", "text": '{"uuid": "v-1", "published": true}'}])


def _ops(feature="agent_prompt_version"):
    return [WriteOp(tool="create_segment_override", project="OpsPilot", environment="Development",
                    feature=feature, segment="tier0_production", value="v1_strict")]


def _approval(ops, approval_id="appr_1", secret=SECRET):
    exp = NOW + timedelta(minutes=10)
    ph = payload_hash(ops)
    a = Approval(proposal_id="prop_1", approval_id=approval_id, approver="naman", payload_hash=ph,
                 signature=sign_approval(secret, approval_id, "prop_1", ph, exp.isoformat()),
                 created_at=NOW, expires_at=exp)
    return Approval.model_validate(a.model_dump(mode="json"))


class FakeMcp:
    def __init__(self, result=OK, exc=None):
        self.result, self.exc, self.sent = result, exc, []

    def call_tool(self, name, arguments, *, is_write):
        self.sent.append((name, arguments, is_write))
        if self.exc:
            raise self.exc
        return self.result


class Fingerprints:
    def __init__(self, *values):
        self.values = list(values)
        self.asked = []

    def __call__(self, environment):
        self.asked.append(environment)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def parts(tmp_path):
    approvals = ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE, clock=lambda: NOW)
    audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: NOW)
    return approvals, audit


def _executor(parts, mcp, fps, settle_reads=1):
    approvals, audit = parts
    return Executor(approvals=approvals, scope=SCOPE, ids=IDS, mcp=mcp, fingerprint=fps,
                    audit=audit, settle_reads=settle_reads, sleep=lambda s: None)


def test_after_fingerprint_waits_for_the_edge_to_catch_up(parts):
    # The edge document lags a write by a few seconds: keep reading until it moves.
    mcp, fps = FakeMcp(), Fingerprints(FP_OK, FP_OK, FP_OK, FP_NEW)
    ops = _ops()
    result = _executor(parts, mcp, fps, settle_reads=5).execute(_approval(ops), ops, FP_OK)
    assert result.fingerprint_after == FP_NEW and result.after_settled


def test_after_fingerprint_gives_up_and_says_so(parts):
    mcp, fps = FakeMcp(), Fingerprints(FP_OK, FP_OK, FP_OK, FP_OK)
    ops = _ops()
    result = _executor(parts, mcp, fps, settle_reads=3).execute(_approval(ops), ops, FP_OK)
    assert result.status == "applied"
    assert result.fingerprint_after == FP_OK and not result.after_settled
    assert parts[1].entries()[-1]["after_settled"] is False


def test_approved_write_is_sent_and_audited(parts):
    mcp, fps = FakeMcp(), Fingerprints(FP_OK, FP_NEW)
    ops = _ops()
    result = _executor(parts, mcp, fps).execute(_approval(ops), ops, FP_OK)
    assert result.status == "applied"
    assert result.fingerprint_before == FP_OK and result.fingerprint_after == FP_NEW
    assert result.version_uuids == ["v-1"]
    assert [(s[0], s[2]) for s in mcp.sent] == [(V2_WRITE_TOOL, True)]
    assert mcp.sent[0][1] == result.calls[0].arguments
    assert fps.asked == ["Development", "Development"]
    entry = parts[1].entries()[-1]
    assert entry["action"] == "write" and entry["approver"] == "naman"
    assert entry["payload_hash"] == payload_hash(ops)
    assert entry["fingerprint_before"] == FP_OK and entry["fingerprint_after"] == FP_NEW
    assert parts[0].is_used("appr_1")


def test_forged_approval_sends_nothing(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approval(ops, secret="x"), ops, FP_OK)
    assert err.value.reason is Refusal.FORGED
    assert mcp.sent == []
    assert parts[1].entries()[-1]["action"] == "refused"


def test_stale_state_sends_nothing_and_keeps_the_approval(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(StaleState):
        _executor(parts, mcp, Fingerprints(FP_NEW)).execute(_approval(ops), ops, FP_OK)
    assert mcp.sent == []
    assert not parts[0].is_used("appr_1")
    assert "stale" in parts[1].entries()[-1]["result"]


def test_reused_approval_writes_once(parts):
    mcp = FakeMcp()
    ops = _ops()
    approval = _approval(ops)
    ex = _executor(parts, mcp, Fingerprints(FP_OK, FP_NEW, FP_OK))
    ex.execute(approval, ops, FP_OK)
    with pytest.raises(ApprovalRefused) as err:
        ex.execute(approval, ops, FP_OK)
    assert err.value.reason is Refusal.REUSED
    assert len(mcp.sent) == 1


def test_tool_error_is_a_failed_write_and_burns_the_approval(parts):
    mcp = FakeMcp(result=ToolResult(True, [{"type": "text", "text": "HTTP error 400"}]))
    ops = _ops()
    with pytest.raises(WriteFailed) as err:
        _executor(parts, mcp, Fingerprints(FP_OK, FP_OK)).execute(_approval(ops), ops, FP_OK)
    assert err.value.result.status == "failed"
    assert parts[0].is_used("appr_1")
    assert parts[1].entries()[-1]["result"].startswith("failed")


def test_network_error_on_write_is_not_retried(parts):
    mcp = FakeMcp(exc=McpError("network error during write (not retried)"))
    ops = _ops()
    with pytest.raises(WriteFailed):
        _executor(parts, mcp, Fingerprints(FP_OK, FP_OK)).execute(_approval(ops), ops, FP_OK)
    assert len(mcp.sent) == 1


def test_unrenderable_ops_send_nothing_and_keep_the_approval(parts):
    mcp = FakeMcp()
    ops = _ops(feature="not_a_flag")
    with pytest.raises(ApprovalRefused) as err:
        _executor(parts, mcp, Fingerprints(FP_OK)).execute(_approval(ops), ops, FP_OK)
    assert err.value.reason is Refusal.BAD_REQUEST
    assert mcp.sent == [] and not parts[0].is_used("appr_1")


def test_fingerprint_unavailable_means_no_write(parts):
    mcp = FakeMcp()
    ops = _ops()
    with pytest.raises(StaleState, match="could not"):
        _executor(parts, mcp, Fingerprints(RuntimeError("edge down"))).execute(
            _approval(ops), ops, FP_OK)
    assert mcp.sent == []


def test_after_fingerprint_failure_does_not_hide_a_successful_write(parts):
    mcp = FakeMcp()
    ops = _ops()
    result = _executor(parts, mcp, Fingerprints(FP_OK, RuntimeError("edge down"))).execute(
        _approval(ops), ops, FP_OK)
    assert result.status == "applied" and result.fingerprint_after is None
