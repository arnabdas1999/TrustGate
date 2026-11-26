"""Hour-one checks against real Flagsmith (run at checkpoints: uv run pytest -m flagsmith).

Development environment only. Every write goes through the real gate path (signed approval,
stale check, v2 write, audit) and is undone at the end of its test.

Needs, in the environment (never in the repo): FLAGSMITH_API_KEY (organisation key),
GATE_SERVER_KEY_DEVELOPMENT (ser. key of Development), GATE_PROJECT_ID, and
GATE_ENVIRONMENTS containing "Development:<id>". Skipped when any is missing.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from contracts.hashing import payload_hash, sign_approval
from contracts.models import Approval, WriteOp
from gate.allowlist import GATE_NATIVE_READS, MCP_READS, MCP_WRITES
from gate.approvals import ApprovalStore
from gate.audit_log import AuditLog
from gate.executor import Executor, StaleState
from gate.fingerprint import canonical_view, fetch_environment_document, fingerprint
from gate.mcp_client import McpClient
from gate.scope import GateScope
from gate.service import GateSettings
from gate.writer import McpIdResolver

pytestmark = pytest.mark.flagsmith

ENV = "Development"
FEATURE, SEGMENT = "agent_prompt_version", "tier0_production"
BASELINE, OVERRIDE = "v1_strict", "v2_fast"
DENIED_READS = {"list_organizations", "list_organization_groups", "list_organization_invites",
                "get_feature_code_references"}
IDENTITIES = {  # traits set in code; prod/tier-1 tells AND from OR
    "tier0": ({"env": "prod", "tier": 0}, True),
    "staging": ({"env": "staging", "tier": 2}, False),
    "prod_tier1": ({"env": "prod", "tier": 1}, False),
}


@pytest.fixture(scope="module")
def live():
    settings = GateSettings.from_env()
    if ENV not in settings.scope.environments or not settings.api_key \
            or ENV not in settings.server_keys:
        pytest.skip("Flagsmith keys / GATE_* settings not set")
    scope = GateScope(settings.scope.project_id, settings.scope.project_name,
                      {ENV: settings.scope.environments[ENV]})  # Development only, always
    mcp = McpClient(settings.mcp_url, settings.api_key)
    server_key = settings.server_keys[ENV]

    def document():
        doc = fetch_environment_document(server_key)
        assert doc["id"] == scope.environments[ENV], "server key is not Development's"
        return doc

    yield {"scope": scope, "mcp": mcp, "server_key": server_key, "document": document,
           "ids": McpIdResolver(mcp, project_id=scope.project_id)}
    mcp.close()


def _executor(live, tmp_path, secret):
    return Executor(approvals=ApprovalStore(tmp_path / "a.db", secret=secret,
                                            scope=live["scope"]),
                    scope=live["scope"], ids=live["ids"], mcp=live["mcp"],
                    fingerprint=lambda env: fingerprint(live["document"]()),
                    audit=AuditLog(tmp_path / "audit.jsonl"))


def _run(executor, ops, secret, fingerprint):
    """Register the proposal at `fingerprint` (what /gate/register records), approve, execute."""
    approval = _approve(ops, secret)
    executor._approvals.register_proposal(approval.proposal_id, approval.payload_hash,
                                          fingerprint, ENV)
    return executor.execute(approval, ops)


def _approve(ops, secret):
    approval_id, proposal_id = f"appr_it_{secrets.token_hex(4)}", f"prop_it_{secrets.token_hex(4)}"
    expires = datetime.now(UTC) + timedelta(minutes=10)
    ph = payload_hash(ops)
    a = Approval(proposal_id=proposal_id, approval_id=approval_id, approver="integration-test",
                 payload_hash=ph, created_at=datetime.now(UTC), expires_at=expires,
                 signature=sign_approval(secret, approval_id, proposal_id, ph,
                                         expires.isoformat()))
    return Approval.model_validate(a.model_dump(mode="json"))


def _op(tool, value=None, segment=SEGMENT, feature=FEATURE, project=None, live=None):
    return WriteOp(tool=tool, project=project or str(live["scope"].project_id), environment=ENV,
                   feature=feature, segment=segment, value=value)


def _identity_value(server_key, identifier, traits, feature):
    r = httpx.post("https://edge.api.flagsmith.com/api/v1/identities/",
                   headers={"X-Environment-Key": server_key},
                   json={"identifier": identifier, "transient": True,
                         "traits": [{"trait_key": k, "trait_value": v} for k, v in traits.items()]},
                   timeout=15)
    r.raise_for_status()
    flags = {f["feature"]["name"]: f for f in r.json()["flags"]}
    return flags[feature]["feature_state_value"]


def _wait_for(check, timeout=60.0):
    start = time.monotonic()
    while True:
        if check():
            return round(time.monotonic() - start, 1)
        if time.monotonic() - start > timeout:
            return None
        time.sleep(2)


def test_mcp_lists_every_allowlisted_read_and_the_v2_write(live):
    names = {t["name"] for t in live["mcp"].list_tools()}
    assert MCP_READS <= names
    assert MCP_WRITES <= names
    assert not (GATE_NATIVE_READS & names), "gate-native reads should not exist in MCP"
    assert DENIED_READS <= names, "denied reads exist, so the allowlist must keep refusing them"


def test_fingerprint_is_stable_across_unchanged_reads(live):
    assert fingerprint(live["document"]()) == fingerprint(live["document"]())


def test_stale_fingerprint_blocks_the_write(live, tmp_path):
    secret = secrets.token_hex(16)
    ops = [_op("create_segment_override", OVERRIDE, live=live)]
    with pytest.raises(StaleState):
        _run(_executor(live, tmp_path, secret), ops, secret, "0" * 64)
    assert canonical_view(live["document"]())["segments"][SEGMENT]["overrides"] == {}


def _settled(live, check=lambda view: True, reads=3, timeout=90.0):
    """Fingerprint once the edge document passes `check` and holds still for `reads` reads.

    The edge document lags a write by a few seconds, so a single read can show the old state.
    """
    seen: list[str] = []
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        doc = live["document"]()
        if check(canonical_view(doc)):
            seen.append(fingerprint(doc))
            if len(seen) >= reads and len(set(seen[-reads:])) == 1:
                return seen[-1]
        else:
            seen.clear()
        time.sleep(2)
    raise AssertionError("environment document did not settle")


def _overrides(view):
    return view["segments"][SEGMENT]["overrides"]


def test_verifier_reads_back_baseline_and_rejects_a_wrong_expectation(live):
    from contracts.models import Identity
    from verifier.readback import EdgeFlagSource, Expectation, VerifyStatus, verify

    source = EdgeFlagSource(live["server_key"])
    tier0 = Identity(identifier="it-verifier-tier0", traits={"env": "prod", "tier": 0})
    staging = Identity(identifier="it-verifier-staging", traits={"env": "staging", "tier": 2})
    ok = verify([Expectation(tier0, {FEATURE: BASELINE, "agent_max_steps": 6}),
                 Expectation(staging, {FEATURE: BASELINE})], source, timeout_s=20)
    assert ok.status is VerifyStatus.VERIFIED, ok.detail
    wrong = verify([Expectation(tier0, {FEATURE: OVERRIDE})], source, timeout_s=4, interval_s=2)
    assert wrong.status is VerifyStatus.FAILED
    missing = verify([Expectation(tier0, {"no_such_flag": "x"})], source, timeout_s=2,
                     interval_s=2)
    assert missing.status is VerifyStatus.TIMEOUT


def test_segment_override_write_readback_and_cleanup(live, tmp_path):
    secret = secrets.token_hex(16)
    executor = _executor(live, tmp_path, secret)
    start_fp = _settled(live)
    if _overrides(canonical_view(live["document"]())):
        pytest.skip(f"{SEGMENT} already has an override in {ENV}; not touching it")

    create = [_op("create_segment_override", OVERRIDE, live=live)]
    applied = False
    try:
        result = _run(executor, create, secret, start_fp)
        applied = True
        assert result.status == "applied" and result.version_uuids
        assert result.after_settled and result.fingerprint_after != start_fp

        def served_as_expected():
            return all(
                _identity_value(live["server_key"], f"it-{name}", traits, FEATURE)
                == (OVERRIDE if in_segment else BASELINE)
                for name, (traits, in_segment) in IDENTITIES.items())
        assert _wait_for(served_as_expected) is not None, "read-back did not converge in 60 s"
    finally:
        # Always undo our own write, from a settled view that shows it.
        if applied or _overrides(canonical_view(live["document"]())):
            current = _settled(live, lambda v: bool(_overrides(v)))
            remove = [_op("delete_feature_segment", live=live)]
            _run(executor, remove, secret, current)
    assert _settled(live, lambda v: not _overrides(v)) == start_fp
    writes = [e for e in executor._audit.entries() if e["action"] == "write"]
    assert [e["result"] for e in writes] == ["applied", "applied"]


def test_environment_default_value_update_and_revert(live, tmp_path):
    secret = secrets.token_hex(16)
    executor = _executor(live, tmp_path, secret)
    feature = "agent_context_mode"
    start_fp = _settled(live)
    original = canonical_view(live["document"]())["features"][feature]["value"]
    changed = "full_history" if original != "full_history" else "summary"

    def value(view):
        return view["features"][feature]["value"]

    update = [_op("update_feature_state", changed, segment=None, feature=feature, live=live)]
    applied = False
    try:
        result = _run(executor, update, secret, start_fp)
        applied = True
        assert result.status == "applied" and result.after_settled
    finally:
        if applied or value(canonical_view(live["document"]())) != original:
            current = _settled(live, lambda v: value(v) == changed)
            revert = [_op("update_feature_state", original, segment=None, feature=feature,
                          live=live)]
            _run(executor, revert, secret, current)
    assert _settled(live, lambda v: value(v) == original) == start_fp
