"""Fixture data so the UI can be built before the real backend exists (MOCK=1).

Shapes follow contracts/ exactly. Numbers here are made up for layout only and must
never appear in the README, the video or results.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from contracts import (
    AuditEntry,
    ConfigSource,
    Domain,
    FlagChange,
    FlagConfig,
    Investigation,
    InvestigationMode,
    InvestigationStatus,
    Proposal,
    ProposalState,
    RepairCandidate,
    RepairKind,
    TestOutcome,
    TestPhase,
    TestResult,
    Timestamps,
    WriteOp,
    payload_hash,
)

BASELINE = {
    "agent_model": "model_flagship",
    "agent_prompt_version": "v1_strict",
    "agent_tool_policy": "restricted",
    "agent_max_steps": 6,
    "agent_context_mode": "summary",
    "agent_temperature": 0.0,
    "agent_safe_fallback": False,
}

RELEASE_CHANGES = [
    FlagChange(flag="agent_model", old="model_flagship", new="model_cheap"),
    FlagChange(flag="agent_prompt_version", old="v1_strict", new="v2_fast"),
    FlagChange(flag="agent_tool_policy", old="restricted", new="expanded"),
    FlagChange(flag="agent_max_steps", old=6, new=12),
    FlagChange(flag="agent_context_mode", old="summary", new="full_history"),
]


def _config(changes: list[FlagChange], overrides: dict | None = None) -> FlagConfig:
    values = dict(BASELINE)
    for change in changes:
        values[change.flag] = change.new
    return FlagConfig(
        values=values, source=ConfigSource.OVERRIDE, segment_overrides=overrides or {}
    )


def _step(phase: TestPhase, changes: list[FlagChange], runs: int, failures: int,
          outcome: TestOutcome, overrides: dict | None = None) -> TestResult:
    return TestResult(
        phase=phase, config=_config(changes, overrides), changes=changes, runs=runs,
        failures=failures, outcome=outcome,
        trace_ids=[f"run_{phase.value}_{i}" for i in range(runs)],
    )


def _by_flag(*flags: str) -> list[FlagChange]:
    return [c for c in RELEASE_CHANGES if c.flag in flags]


def build(now: datetime | None = None) -> tuple[dict[str, Investigation], dict[str, Proposal],
                                                list[AuditEntry]]:
    now = now or datetime.now(UTC)
    alert = now - timedelta(minutes=4)

    # --- OpsPilot: single-flag culprit, waiting for approval -------------------------------
    prompt = _by_flag("agent_prompt_version")
    tier0 = {"tier0_production": {"agent_prompt_version": "v1_strict"}}
    steps = [
        _step(TestPhase.REPRODUCE, RELEASE_CHANGES, 10, 9, TestOutcome.FAIL),
        _step(TestPhase.REPRODUCE, [], 10, 0, TestOutcome.PASS),
        _step(TestPhase.SEARCH, _by_flag("agent_prompt_version", "agent_tool_policy"), 3, 2,
              TestOutcome.FAIL),
        _step(TestPhase.SEARCH, _by_flag("agent_model", "agent_max_steps", "agent_context_mode"),
              3, 0, TestOutcome.INCONCLUSIVE),
        _step(TestPhase.ESCALATE,
              _by_flag("agent_model", "agent_max_steps", "agent_context_mode"), 10, 0,
              TestOutcome.PASS),
        _step(TestPhase.SEARCH, prompt, 3, 3, TestOutcome.FAIL),
        _step(TestPhase.SEARCH, _by_flag("agent_tool_policy"), 3, 0, TestOutcome.INCONCLUSIVE),
        _step(TestPhase.CONFIRM, prompt, 10, 8, TestOutcome.FAIL),
        _step(TestPhase.CONFIRM, [], 10, 0, TestOutcome.PASS),
        _step(TestPhase.VALIDATE, RELEASE_CHANGES, 20, 0, TestOutcome.PASS, overrides=tier0),
    ]
    segment_ops = [WriteOp(tool="create_segment_override", project="opspilot",
                           environment="demo-production", feature="agent_prompt_version",
                           segment="tier0_production", value="v1_strict")]
    surgical_ops = [WriteOp(tool="update_feature_state", project="opspilot",
                            environment="demo-production", feature="agent_prompt_version",
                            value="v1_strict")]
    global_ops = [WriteOp(tool="update_feature_state", project="opspilot",
                          environment="demo-production", feature=c.flag, value=c.old)
                  for c in RELEASE_CHANGES]
    candidates = [
        RepairCandidate(kind=RepairKind.SEGMENT_OVERRIDE, ops=segment_ops, validation=steps[-1],
                        heldout_passes=20, heldout_runs=21, baseline_heldout_passes=20,
                        cost_kept=0.94, violations=0),
        RepairCandidate(kind=RepairKind.SURGICAL, ops=surgical_ops, heldout_passes=18,
                        heldout_runs=21, baseline_heldout_passes=20, cost_kept=0.91, violations=0),
        RepairCandidate(kind=RepairKind.GLOBAL, ops=global_ops, heldout_passes=20,
                        heldout_runs=21, baseline_heldout_passes=20, cost_kept=0.0, violations=0),
    ]
    ops_inv = Investigation(
        id="inv_opspilot_demo", incident_id="inc_opspilot_demo", domain=Domain.OPSPILOT,
        mode=InvestigationMode.GUIDED, status=InvestigationStatus.PROPOSED,
        segment="tier0_production",
        suspects=_by_flag("agent_prompt_version", "agent_tool_policy", "agent_max_steps",
                          "agent_context_mode", "agent_model"),
        steps=steps, culprit_set=prompt, candidates=candidates,
        timestamps=Timestamps(alert=alert, first_suspect=alert + timedelta(seconds=48),
                              proposal=now - timedelta(seconds=20)),
        runs_used=sum(s.runs for s in steps), usd_used=1.84, proposal_id="prop_opspilot_demo",
    )
    ops_prop = Proposal(
        id="prop_opspilot_demo", investigation_id=ops_inv.id, candidate=candidates[0],
        payload_hash=payload_hash(segment_ops), fingerprint="mock-fingerprint-0000",
        explanation=(
            "The failure appears only when agent_prompt_version is v2_fast. Its prompt tells the "
            "agent to 'take decisive action', and on payments-prod that led to an unapproved "
            "force_restart followed by a false RESOLVED status. Staging allows restarts without "
            "a change window, so v2_fast stays useful there. Overriding the prompt to v1_strict "
            "for tier0_production only keeps the cheaper model and the faster prompt everywhere "
            "else."
        ),
        state=ProposalState.PROPOSED, created_at=now - timedelta(seconds=20),
        expires_at=now + timedelta(minutes=15),
    )

    # --- VaultGuard: two-flag interaction, still searching ---------------------------------
    vg_inv = Investigation(
        id="inv_vaultguard_demo", incident_id="inc_vaultguard_demo", domain=Domain.VAULTGUARD,
        status=InvestigationStatus.SEARCHING, segment="high_value_wires",
        suspects=_by_flag("agent_tool_policy", "agent_prompt_version", "agent_model",
                          "agent_max_steps", "agent_context_mode"),
        steps=[
            _step(TestPhase.REPRODUCE, RELEASE_CHANGES, 10, 8, TestOutcome.FAIL),
            _step(TestPhase.REPRODUCE, [], 10, 0, TestOutcome.PASS),
            _step(TestPhase.SEARCH, _by_flag("agent_tool_policy"), 3, 0,
                  TestOutcome.INCONCLUSIVE),
            _step(TestPhase.SEARCH, _by_flag("agent_prompt_version"), 3, 0,
                  TestOutcome.INCONCLUSIVE),
        ],
        timestamps=Timestamps(alert=now - timedelta(minutes=1),
                              first_suspect=now - timedelta(seconds=30)),
        runs_used=26, usd_used=0.61,
    )

    audit = [
        AuditEntry(ts=alert + timedelta(seconds=2), actor="investigator", action="read",
                   tool="get_audit_log", result="5 flag changes in the last 12 h"),
        AuditEntry(ts=alert + timedelta(seconds=3), actor="investigator", action="read",
                   tool="list_project_features", args={"project": "opspilot"}, result="7 features"),
        AuditEntry(ts=alert + timedelta(seconds=4), actor="investigator", action="read",
                   tool="list_project_segments", args={"project": "opspilot"},
                   result="tier0_production, high_value_wires"),
        AuditEntry(ts=alert + timedelta(seconds=5), actor="investigator", action="read",
                   tool="get_project_segment", args={"segment": "tier0_production"},
                   result="env == prod AND tier == 0"),
        AuditEntry(ts=alert + timedelta(seconds=6), actor="investigator", action="refused",
                   tool="list_organization_invites", result="not on the read allowlist"),
    ]

    investigations = {ops_inv.id: ops_inv, vg_inv.id: vg_inv}
    proposals = {ops_prop.id: ops_prop}
    return investigations, proposals, audit
