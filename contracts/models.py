"""Shared data contracts for every TrustGate component.

Draft from the Development plan. Naman and Khyati review before CP0 is tagged.
Changes after that need a PR approved by Arnab and the affected owner.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

# A flag value as Flagsmith serves it: plain string, number or boolean (no multivariate weights).
FlagValue = bool | int | float | str


class Domain(StrEnum):
    OPSPILOT = "opspilot"
    VAULTGUARD = "vaultguard"


# ---------------------------------------------------------------------------
# Configuration and identity
# ---------------------------------------------------------------------------


class ConfigSource(StrEnum):
    CAPTURED = "captured"  # what the failing agent actually received
    BASELINE = "baseline"  # from baseline_env.json
    OVERRIDE = "override"  # built by search or repair for a replay


class FlagConfig(BaseModel):
    """A complete set of flag values for one replay.

    segment_overrides lets a replay validate a segment-scoped repair: the sandbox applies
    an override only when the identity's traits match that segment.
    """

    values: dict[str, FlagValue]
    source: ConfigSource
    segment_overrides: dict[str, dict[str, FlagValue]] = Field(default_factory=dict)

    # Flagsmith remote config may hand numbers over as strings (agent_temperature is "0.0"),
    # so consumers read typed values through these helpers instead of casting by hand.
    def get_str(self, flag: str) -> str:
        return str(self.values[flag])

    def get_float(self, flag: str) -> float:
        return float(self.values[flag])

    def get_int(self, flag: str) -> int:
        value = self.values[flag]
        if isinstance(value, bool):
            raise TypeError(f"{flag} is a boolean, not an integer")
        return int(float(value)) if isinstance(value, str) else int(value)

    def get_bool(self, flag: str) -> bool:
        value = self.values[flag]
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise TypeError(f"{flag}={value!r} is not a boolean")


class Identity(BaseModel):
    """Who the agent acts for. Traits are set by application code, never by users or the LLM."""

    identifier: str
    traits: dict[str, FlagValue] = Field(default_factory=dict)


class FlagChange(BaseModel):
    flag: str
    old: FlagValue | None = None
    new: FlagValue | None = None


# ---------------------------------------------------------------------------
# Runs and grading
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    ts: datetime


class Trace(BaseModel):
    run_id: str
    domain: Domain
    case_id: str
    flags: FlagConfig
    identity: Identity
    tool_calls: list[ToolCall] = Field(default_factory=list)
    reported_status: str | None = None  # from the agent's report_status tool, never free text
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str | None = None


class GradeResult(BaseModel):
    """Deterministic grade from database state and tool logs. No LLM."""

    violations: list[str] = Field(default_factory=list)
    success: bool  # the right next action happened (escalate, request approval, ...)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return not self.violations and self.success


class Incident(BaseModel):
    id: str
    domain: Domain
    case_id: str
    identity: Identity
    resolved_flags: FlagConfig
    baseline_snapshot: str  # path or ref to baseline_env.json
    release_snapshot: str
    db_snapshot: str
    trace: Trace
    created_at: datetime


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestOutcome(StrEnum):
    __test__ = False  # not a pytest test class

    PASS = "PASS"  # noqa: S105 (a test outcome, not a password)
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class TestPhase(StrEnum):
    __test__ = False

    REPRODUCE = "reproduce"
    SEARCH = "search"
    ESCALATE = "escalate"
    CONFIRM = "confirm"
    VALIDATE = "validate"
    POST_WRITE = "post_write"


class TestResult(BaseModel):
    """One configuration tested N times. One violation is a FAIL; a clean result is statistical."""

    __test__ = False

    phase: TestPhase
    config: FlagConfig
    # Which release changes this configuration applied (drives the UI's search matrix)
    changes: list[FlagChange] = Field(default_factory=list)
    runs: int
    failures: int
    outcome: TestOutcome
    trace_ids: list[str] = Field(default_factory=list)
    fresh: bool = True  # False only for cached results shown for display

    @model_validator(mode="after")
    def _consistent(self) -> TestResult:
        if self.failures > self.runs:
            raise ValueError("failures cannot exceed runs")
        if self.outcome == TestOutcome.FAIL and self.failures == 0:
            raise ValueError("a FAIL needs at least one observed violation")
        if self.outcome == TestOutcome.PASS and self.failures > 0:
            raise ValueError("a PASS cannot contain violations")
        return self


# ---------------------------------------------------------------------------
# Repair, proposals and approvals
# ---------------------------------------------------------------------------

WriteTool = Literal["create_segment_override", "update_feature_state", "delete_feature_segment"]


class WriteOp(BaseModel):
    """One Flagsmith write. Built by code from a validated RepairCandidate, never by the LLM."""

    tool: WriteTool
    project: str
    environment: str
    feature: str
    segment: str | None = None
    value: FlagValue | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _segment_rules(self) -> WriteOp:
        needs_segment = self.tool in ("create_segment_override", "delete_feature_segment")
        if needs_segment and not self.segment:
            raise ValueError(f"{self.tool} needs a segment")
        if self.tool == "update_feature_state" and self.segment:
            raise ValueError("update_feature_state changes the environment default; use no segment")
        return self


class RepairKind(StrEnum):
    SEGMENT_OVERRIDE = "segment_override"
    SURGICAL = "surgical"
    GLOBAL = "global"


class RepairCandidate(BaseModel):
    kind: RepairKind
    ops: list[WriteOp]
    validation: TestResult | None = None
    heldout_passes: int | None = None
    heldout_runs: int | None = None
    baseline_heldout_passes: int | None = None
    # Share of the release's cost saving this repair keeps, 0 to 1
    cost_kept: float | None = Field(default=None, ge=0.0, le=1.0)
    violations: int | None = None


class InvestigationMode(StrEnum):
    GUIDED = "guided"  # LLM orders suspects
    PLAIN = "plain"  # plain ddmin, benchmark arm


class InvestigationStatus(StrEnum):
    CAPTURING = "capturing"
    REPRODUCING = "reproducing"
    SEARCHING = "searching"
    VALIDATING = "validating"
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLIED = "applied"
    VERIFIED = "verified"
    STALE = "stale"
    EXPIRED = "expired"
    FAILED = "failed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    NOT_REPRODUCIBLE = "not_reproducible"


class Timestamps(BaseModel):
    alert: datetime
    first_suspect: datetime | None = None
    proposal: datetime | None = None
    approved: datetime | None = None
    verified: datetime | None = None


class Investigation(BaseModel):
    id: str
    incident_id: str
    domain: Domain
    mode: InvestigationMode = InvestigationMode.GUIDED
    status: InvestigationStatus
    segment: str | None = None  # matched by deterministic triage, e.g. tier0_production
    suspects: list[FlagChange] = Field(default_factory=list)  # ordered, most likely first
    steps: list[TestResult] = Field(default_factory=list)
    culprit_set: list[FlagChange] = Field(default_factory=list)
    candidates: list[RepairCandidate] = Field(default_factory=list)
    timestamps: Timestamps
    runs_used: int = 0
    usd_used: float = 0.0
    budget_runs: int = 240
    budget_usd: float = 5.0
    proposal_id: str | None = None


class ProposalState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLIED = "applied"
    VERIFIED = "verified"
    STALE = "stale"
    EXPIRED = "expired"
    FAILED = "failed"
    REJECTED = "rejected"


class ReadBack(BaseModel):
    """What Flagsmith actually serves after the write, per test identity."""

    identity: Identity
    values: dict[str, FlagValue]
    # True means Flagsmith was not reached (is_default): never counts as verified
    from_default_handler: bool = False


class Proposal(BaseModel):
    id: str
    investigation_id: str
    candidate: RepairCandidate
    payload_hash: str  # contracts.hashing.payload_hash(candidate.ops)
    fingerprint: str  # canonical environment fingerprint at proposal time
    explanation: str  # written by the LLM from structured results; never used to build ops
    state: ProposalState
    created_at: datetime
    expires_at: datetime
    readback: list[ReadBack] = Field(default_factory=list)


class Approval(BaseModel):
    proposal_id: str
    approval_id: str  # single use
    approver: str
    payload_hash: str
    signature: str  # HMAC from the API, checked by the gate
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None


# ---------------------------------------------------------------------------
# API-only models
# ---------------------------------------------------------------------------


class SessionRequest(BaseModel):
    password: str


class SessionResponse(BaseModel):
    token: str
    approver: str
    expires_at: datetime


class StartInvestigationRequest(BaseModel):
    incident_id: str
    mode: InvestigationMode = InvestigationMode.GUIDED


class AuditEntry(BaseModel):
    """One line of the gate's append-only audit trail, as the UI shows it."""

    ts: datetime
    actor: str  # "investigator", "executor", or the approver's name
    action: Literal["read", "write", "refused", "approve", "reject"]
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    proposal_id: str | None = None


class ActivityPing(BaseModel):
    """Sent by the UI's active-time logger for the human-attention metric."""

    investigation_id: str
    active_ms: int = Field(ge=0)
    visible: bool
    ts: datetime
