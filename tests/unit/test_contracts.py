from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from contracts import (
    ConfigSource,
    FlagChange,
    FlagConfig,
    GradeResult,
    Identity,
    Investigation,
    InvestigationStatus,
    TestOutcome,
    TestPhase,
    TestResult,
    Timestamps,
    WriteOp,
    payload_hash,
    sign_approval,
    verify_signature,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _config(**values):
    return FlagConfig(
        values=values or {"agent_prompt_version": "v2_fast"}, source=ConfigSource.OVERRIDE
    )


def test_flag_values_keep_their_types():
    config = FlagConfig(
        values={"agent_max_steps": 12, "agent_temperature": 0.0, "agent_safe_fallback": False,
                "agent_model": "model_cheap"},
        source=ConfigSource.CAPTURED,
    )
    restored = FlagConfig.model_validate_json(config.model_dump_json())
    assert restored.values["agent_max_steps"] == 12
    assert isinstance(restored.values["agent_safe_fallback"], bool)
    assert restored.values["agent_model"] == "model_cheap"


def test_typed_getters_parse_remote_config_strings():
    config = FlagConfig(
        values={"agent_temperature": "0.0", "agent_max_steps": "12", "agent_safe_fallback": False,
                "flag_text": "true"},
        source=ConfigSource.CAPTURED,
    )
    assert config.get_float("agent_temperature") == 0.0
    assert config.get_int("agent_max_steps") == 12
    assert config.get_bool("agent_safe_fallback") is False
    assert config.get_bool("flag_text") is True
    with pytest.raises(TypeError):
        config.get_int("agent_safe_fallback")
    with pytest.raises(TypeError):
        config.get_bool("agent_max_steps")


def test_grade_passed_needs_success_and_no_violations():
    assert GradeResult(violations=[], success=True).passed is True
    assert GradeResult(violations=["false_resolution"], success=True).passed is False
    assert GradeResult(violations=[], success=False).passed is False
    assert GradeResult(violations=[], success=True).model_dump()["passed"] is True


def test_fail_needs_an_observed_violation():
    with pytest.raises(ValidationError):
        TestResult(phase=TestPhase.SEARCH, config=_config(), runs=3, failures=0,
                   outcome=TestOutcome.FAIL)


def test_pass_cannot_contain_violations():
    with pytest.raises(ValidationError):
        TestResult(phase=TestPhase.SEARCH, config=_config(), runs=3, failures=1,
                   outcome=TestOutcome.PASS)


def test_failures_cannot_exceed_runs():
    with pytest.raises(ValidationError):
        TestResult(phase=TestPhase.SEARCH, config=_config(), runs=3, failures=4,
                   outcome=TestOutcome.FAIL)


def test_inconclusive_allows_zero_failures():
    result = TestResult(phase=TestPhase.ESCALATE, config=_config(), runs=3, failures=0,
                        outcome=TestOutcome.INCONCLUSIVE)
    assert result.outcome == TestOutcome.INCONCLUSIVE


def test_segment_override_needs_a_segment():
    with pytest.raises(ValidationError):
        WriteOp(tool="create_segment_override", project="p", environment="e",
                feature="agent_prompt_version", value="v1_strict")


def test_update_feature_state_takes_no_segment():
    with pytest.raises(ValidationError):
        WriteOp(tool="update_feature_state", project="p", environment="e",
                feature="agent_prompt_version", segment="tier0_production", value="v1_strict")


def test_unknown_write_tool_is_rejected():
    with pytest.raises(ValidationError):
        WriteOp(tool="delete_feature", project="p", environment="e", feature="x")


def _ops(value="v1_strict"):
    return [WriteOp(tool="create_segment_override", project="opspilot", environment="prod",
                    feature="agent_prompt_version", segment="tier0_production", value=value)]


def test_payload_hash_is_stable_and_sensitive():
    assert payload_hash(_ops()) == payload_hash(_ops())
    assert payload_hash(_ops()) != payload_hash(_ops("v2_fast"))
    assert len(payload_hash(_ops())) == 64


def test_signature_round_trip_and_tamper_detection():
    expires = (NOW + timedelta(minutes=15)).isoformat()
    payload = payload_hash(_ops())
    sig = sign_approval("s3cret", "appr_1", "prop_1", payload, expires)
    assert verify_signature("s3cret", sig, "appr_1", "prop_1", payload, expires)
    assert not verify_signature("other", sig, "appr_1", "prop_1", payload, expires)
    assert not verify_signature("s3cret", sig, "appr_1", "prop_1", payload_hash(_ops("v2_fast")),
                                expires)
    assert not verify_signature("s3cret", sig, "appr_2", "prop_1", payload, expires)


def test_signing_without_secret_refuses():
    with pytest.raises(ValueError):
        sign_approval("", "a", "p", "h", "t")


def test_investigation_round_trip():
    inv = Investigation(
        id="inv_1", incident_id="inc_1", domain="opspilot", status=InvestigationStatus.SEARCHING,
        suspects=[FlagChange(flag="agent_prompt_version", old="v1_strict", new="v2_fast")],
        timestamps=Timestamps(alert=NOW),
    )
    restored = Investigation.model_validate_json(inv.model_dump_json())
    assert restored == inv
    assert restored.suspects[0].new == "v2_fast"


def test_identity_traits_default_empty():
    assert Identity(identifier="payments-prod").traits == {}
