from datetime import UTC, datetime, timedelta

import pytest

from contracts.hashing import payload_hash, sign_approval
from contracts.models import Approval, WriteOp
from gate.approvals import ApprovalRefused, ApprovalStore, Refusal
from gate.scope import GateScope

SECRET = "test-signing-secret"
NOW = datetime(2026, 11, 26, 18, 0, 0, 123456, tzinfo=UTC)
SCOPE = GateScope(project_id=56664, project_name="OpsPilot",
                  environments={"Development": 121197, "Demo Production": 121198})


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def _ops(value="v1_strict", environment="Development", project="OpsPilot"):
    return [WriteOp(tool="create_segment_override", project=project, environment=environment,
                    feature="agent_prompt_version", segment="tier0_production", value=value)]


def _approval(ops=None, *, approval_id="appr_1", expires_at=None, secret=SECRET,
              payload=None):
    """Sign exactly like api.main._send_to_gate, then send it over the wire as JSON."""
    ops = ops if ops is not None else _ops()
    expires_at = expires_at or NOW + timedelta(minutes=15)
    payload = payload or payload_hash(ops)
    approval = Approval(
        proposal_id="prop_1", approval_id=approval_id, approver="naman", payload_hash=payload,
        signature=sign_approval(secret, approval_id, "prop_1", payload, expires_at.isoformat()),
        created_at=NOW, expires_at=expires_at)
    wire = approval.model_dump(mode="json")  # expires_at becomes "...Z" here
    return Approval.model_validate(wire)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return ApprovalStore(tmp_path / "approvals.db", secret=SECRET, scope=SCOPE, clock=clock)


def _refusal(store, approval, ops):
    with pytest.raises(ApprovalRefused) as err:
        store.consume(approval, ops)
    return err.value.reason


def test_valid_approval_is_accepted_once(store):
    ops = _ops()
    record = store.consume(_approval(ops), ops)
    assert record.approval_id == "appr_1"
    assert record.approver_claimed == "naman"  # not signed yet: stored as a claim
    assert record.used_at == NOW
    assert store.is_used("appr_1")


def test_signature_survives_the_wire_z_suffix(store):
    # The API signs "+00:00"; JSON carries "Z". The gate must still verify it.
    ops = _ops()
    approval = _approval(ops)
    assert approval.model_dump(mode="json")["expires_at"].endswith("Z")
    store.consume(approval, ops)


def test_forged_signature_is_refused(store):
    ops = _ops()
    assert _refusal(store, _approval(ops, secret="attacker-secret"), ops) is Refusal.FORGED


def test_tampered_expiry_is_refused(store):
    ops = _ops()
    approval = _approval(ops).model_copy(update={"expires_at": NOW + timedelta(minutes=14)})
    assert _refusal(store, approval, ops) is Refusal.FORGED


def test_tampered_approval_id_is_refused(store):
    ops = _ops()
    approval = _approval(ops).model_copy(update={"approval_id": "appr_2"})
    assert _refusal(store, approval, ops) is Refusal.FORGED


def test_reused_approval_is_refused(store):
    ops = _ops()
    approval = _approval(ops)
    store.consume(approval, ops)
    assert _refusal(store, approval, ops) is Refusal.REUSED


def test_reuse_is_refused_even_if_the_request_claims_unused(store):
    ops = _ops()
    approval = _approval(ops)
    store.consume(approval, ops)
    claims_unused = approval.model_copy(update={"used_at": None})
    assert _refusal(store, claims_unused, ops) is Refusal.REUSED


def test_reuse_is_refused_after_a_gate_restart(tmp_path, clock):
    ops = _ops()
    approval = _approval(ops)
    ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE, clock=clock).consume(approval, ops)
    restarted = ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE, clock=clock)
    assert _refusal(restarted, approval, ops) is Refusal.REUSED


def test_expired_approval_is_refused(store, clock):
    ops = _ops()
    approval = _approval(ops)
    clock.now = NOW + timedelta(minutes=15)
    assert _refusal(store, approval, ops) is Refusal.EXPIRED


def test_expiry_too_far_in_the_future_is_refused(store):
    ops = _ops()
    approval = _approval(ops, expires_at=NOW + timedelta(minutes=16))
    assert _refusal(store, approval, ops) is Refusal.EXPIRY_TOO_FAR


def test_changed_payload_is_refused(store):
    approved = _ops("v1_strict")
    sent = _ops("v2_fast")
    assert _refusal(store, _approval(approved), sent) is Refusal.PAYLOAD_MISMATCH


def test_extra_op_is_refused(store):
    approved = _ops()
    sent = approved + _ops("v2_fast")
    assert _refusal(store, _approval(approved), sent) is Refusal.PAYLOAD_MISMATCH


def test_wrong_environment_is_refused_even_when_signed(store):
    ops = _ops(environment="Production")
    assert _refusal(store, _approval(ops), ops) is Refusal.OUT_OF_SCOPE


def test_bench_environment_outside_scope_is_refused(store):
    ops = _ops(environment="bench-1")
    assert _refusal(store, _approval(ops), ops) is Refusal.OUT_OF_SCOPE


def test_wrong_project_is_refused(store):
    ops = _ops(project="VaultGuard")
    assert _refusal(store, _approval(ops), ops) is Refusal.OUT_OF_SCOPE


def test_empty_ops_are_refused(store):
    assert _refusal(store, _approval([]), []) is Refusal.BAD_REQUEST


def test_refused_attempt_does_not_burn_the_approval(store):
    ops = _ops()
    approval = _approval(ops)
    assert _refusal(store, approval, _ops("v2_fast")) is Refusal.PAYLOAD_MISMATCH
    store.consume(approval, ops)  # the genuine request still works


def test_naive_expiry_is_refused(store):
    ops = _ops()
    approval = _approval(ops).model_copy(update={"expires_at": NOW.replace(tzinfo=None)})
    assert _refusal(store, approval, ops) is Refusal.BAD_REQUEST


def test_store_creates_its_folder(tmp_path, clock):
    ApprovalStore(tmp_path / "data" / "gate" / "a.db", secret=SECRET, scope=SCOPE, clock=clock)
    assert (tmp_path / "data" / "gate" / "a.db").exists()


def test_store_needs_a_secret(tmp_path):
    with pytest.raises(ValueError, match="APPROVAL_SIGNING_SECRET"):
        ApprovalStore(tmp_path / "a.db", secret="", scope=SCOPE)


def test_refusal_messages_do_not_include_the_secret(store):
    ops = _ops()
    with pytest.raises(ApprovalRefused) as err:
        store.consume(_approval(ops, secret="attacker-secret"), ops)
    assert SECRET not in str(err.value)


def test_second_approval_for_the_same_proposal_is_refused(store):
    ops = _ops()
    store.consume(_approval(ops, approval_id="appr_1"), ops)
    assert _refusal(store, _approval(ops, approval_id="appr_2"), ops) is Refusal.REUSED


def test_register_and_read_back_a_proposal(store):
    store.register_proposal("prop_1", "h" * 64, "f" * 64, "Development")
    reg = store.registration("prop_1")
    assert (reg.payload_hash, reg.fingerprint, reg.environment) == ("h" * 64, "f" * 64,
                                                                     "Development")
    assert store.registration("prop_2") is None


def test_registration_is_immutable(store):
    store.register_proposal("prop_1", "h" * 64, "f" * 64, "Development")
    store.register_proposal("prop_1", "h" * 64, "f" * 64, "Development")  # same: fine
    from gate.approvals import RegistrationConflict
    with pytest.raises(RegistrationConflict, match="already registered"):
        store.register_proposal("prop_1", "x" * 64, "f" * 64, "Development")


def test_written_over_fingerprint_is_remembered_per_environment(tmp_path, clock):
    path = tmp_path / "a.db"
    s1 = ApprovalStore(path, secret=SECRET, scope=SCOPE, clock=clock)
    s1.mark_written_over("Development", "f" * 64)
    s2 = ApprovalStore(path, secret=SECRET, scope=SCOPE, clock=clock)
    assert s2.was_written_over("Development", "f" * 64)
    assert not s2.was_written_over("Demo Production", "f" * 64)
    s2.mark_written_over("Development", "g" * 64)  # only the last one counts
    assert not s2.was_written_over("Development", "f" * 64)


def test_written_over_record_expires_after_the_edge_lag_window(tmp_path):
    clock = Clock()
    store = ApprovalStore(tmp_path / "a.db", secret=SECRET, scope=SCOPE, clock=clock)
    store.mark_written_over("Development", "f" * 64)
    clock.now = NOW + timedelta(seconds=90)
    assert store.was_written_over("Development", "f" * 64)  # edge may still lag
    clock.now = NOW + timedelta(minutes=3)
    # Flagsmith really is back at that state (e.g. a revert): new proposals may use it.
    assert not store.was_written_over("Development", "f" * 64)


def test_concurrent_conflicting_registrations_give_one_winner(store):
    import threading

    from gate.approvals import RegistrationConflict

    barrier, outcomes = threading.Barrier(2), []

    def register(h):
        barrier.wait()
        try:
            store.register_proposal("prop_r", h * 64, "f" * 64, "Development")
            outcomes.append("ok")
        except RegistrationConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=register, args=(h,)) for h in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert sorted(outcomes) == ["conflict", "ok"]


def test_registration_conflict_is_its_own_error_type(store):
    from gate.approvals import RegistrationConflict

    store.register_proposal("prop_1", "h" * 64, "f" * 64, "Development")
    with pytest.raises(RegistrationConflict):
        store.register_proposal("prop_1", "x" * 64, "f" * 64, "Development")
