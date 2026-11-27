from contracts.models import WriteOp
from verifier.readback import VerifyStatus
from verifier.structure import confirm_written, structure_problems


def _op(tool, feature="agent_prompt_version", value="v1_strict", segment="tier0_production",
        enabled=None):
    return WriteOp(tool=tool, project="TrustGate", environment="bench-1", feature=feature,
                   segment=segment if tool != "update_feature_state" else None, value=value,
                   enabled=enabled)


def _view(overrides=None, features=None):
    return {"features": {k: {"enabled": True, "value": v, "multivariate": []}
                         for k, v in (features or {"agent_max_steps": "6"}).items()},
            "segments": {"tier0_production": {"rules": [], "overrides": {
                f: {"enabled": e, "value": v, "multivariate": [], "priority": 0}
                for f, (v, e) in (overrides or {}).items()}}}}


def test_override_present_with_value_and_enabled():
    assert structure_problems([_op("create_segment_override")],
                              _view({"agent_prompt_version": ("v1_strict", True)})) == []


def test_override_missing_wrong_value_or_disabled():
    op = _op("create_segment_override")
    assert structure_problems([op], _view()) == [
        "override for agent_prompt_version on tier0_production not found in the environment "
        "document"]
    assert "expected 'v1_strict'" in structure_problems(
        [op], _view({"agent_prompt_version": ("v2_fast", True)}))[0]
    assert "enabled=False" in structure_problems(
        [op], _view({"agent_prompt_version": ("v1_strict", False)}))[0]


def test_unknown_segment_counts_as_missing():
    op = _op("create_segment_override", segment="high_value_wires")
    assert "not found" in structure_problems([op], _view())[0]


def test_default_update_compares_typed_values_loosely():
    op = _op("update_feature_state", feature="agent_max_steps", value=6)
    assert structure_problems([op], _view()) == []  # the document says "6"
    bad = _op("update_feature_state", feature="agent_max_steps", value=12)
    assert structure_problems([bad], _view()) == [
        "agent_max_steps default is '6', expected 12"]


def test_delete_requires_the_override_to_be_gone():
    op = _op("delete_feature_segment", value=None)
    assert structure_problems([op], _view()) == []
    assert "still in the environment document" in structure_problems(
        [op], _view({"agent_prompt_version": ("v1_strict", True)}))[0]


def test_confirm_written_polls_until_the_document_catches_up():
    views = [_view(), _view(), _view({"agent_prompt_version": ("v1_strict", True)})]
    slept = []
    result = confirm_written([_op("create_segment_override")], lambda: views.pop(0),
                             timeout_s=60, interval_s=3, clock=lambda: 0.0, sleep=slept.append)
    assert result.status is VerifyStatus.VERIFIED and slept == [3, 3]


def test_confirm_written_fails_with_the_reason_at_the_timeout():
    t = [0.0]

    def sleep(s):
        t[0] += s
    result = confirm_written([_op("create_segment_override")], _view, timeout_s=6,
                             interval_s=3, clock=lambda: t[0], sleep=sleep)
    assert result.status is VerifyStatus.FAILED and "not found" in result.detail


def test_confirm_written_read_errors_are_unverified_never_verified():
    def down():
        raise RuntimeError("gate down")
    result = confirm_written([_op("create_segment_override")], down, timeout_s=0,
                             clock=lambda: 0.0, sleep=lambda s: None)
    assert result.status is VerifyStatus.TIMEOUT and "gate down" in result.detail
