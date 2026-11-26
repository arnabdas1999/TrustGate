"""Builds the proposal a human approves. Code only: the LLM never writes a payload.

The search validates repair candidates (narrowest first). This module:
- picks the narrowest candidate that passed validation
- builds its WriteOps from the candidate's kind, the culprit set and the segment, using the
  typed baseline values from the release diff
- binds the proposal to payload_hash(ops), the environment's fingerprint (read until it holds
  still, since the edge document lags writes) and a 15-minute expiry
- asks the gate to render the exact MCP call(s), which the proposal card shows
- builds inverse ops for a rollback proposal and the verifier's expectations
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.hashing import payload_hash
from contracts.models import (
    FlagChange,
    FlagValue,
    Identity,
    Proposal,
    ProposalState,
    RepairCandidate,
    RepairKind,
    TestOutcome,
    WriteOp,
)
from verifier.readback import Expectation

PROPOSAL_TTL = timedelta(minutes=15)
NARROWEST_FIRST = [RepairKind.SEGMENT_OVERRIDE, RepairKind.SURGICAL, RepairKind.GLOBAL]


class ProposalError(Exception):
    pass


def choose_candidate(candidates: Sequence[RepairCandidate]) -> RepairCandidate | None:
    for kind in NARROWEST_FIRST:
        for cand in candidates:
            if cand.kind is kind and cand.validation is not None \
                    and cand.validation.outcome is TestOutcome.PASS and not cand.violations:
                return cand
    return None


def _old_value(change: FlagChange) -> FlagValue:
    if change.old is None:
        raise ProposalError(f"{change.flag} did not exist before the release; "
                            "it can't be reverted by value")
    return change.old


def build_ops(kind: RepairKind, culprit_set: Sequence[FlagChange],
              suspects: Sequence[FlagChange], *, segment: str | None, project: str,
              environment: str) -> list[WriteOp]:
    base = {"project": project, "environment": environment}
    if kind is RepairKind.SEGMENT_OVERRIDE:
        if not segment:
            raise ProposalError("a segment override needs the incident's segment (triage)")
        return [WriteOp(tool="create_segment_override", feature=c.flag, segment=segment,
                        value=_old_value(c), **base) for c in sorted(culprit_set,
                                                                     key=lambda c: c.flag)]
    changes = culprit_set if kind is RepairKind.SURGICAL else suspects
    return [WriteOp(tool="update_feature_state", feature=c.flag, value=_old_value(c), **base)
            for c in sorted(changes, key=lambda c: c.flag)]


def inverse_ops(ops: Sequence[WriteOp], release_values: Mapping[str, Any]) -> list[WriteOp]:
    """Undo an applied write: remove overrides we created, put back the release values."""
    inverse = []
    for op in ops:
        base = {"project": op.project, "environment": op.environment, "feature": op.feature}
        if op.tool == "create_segment_override":
            inverse.append(WriteOp(tool="delete_feature_segment", segment=op.segment, **base))
        elif op.tool == "update_feature_state":
            inverse.append(WriteOp(tool="update_feature_state",
                                   value=release_values[op.feature], **base))
        else:
            raise ProposalError(f"no inverse for {op.tool}")
    return inverse


def settled_fingerprint(gate: Any, environment: str, *, attempts: int = 15,
                        interval_s: float = 2.0,
                        sleep: Callable[[float], None] = time.sleep) -> str:
    previous = None
    for attempt in range(attempts):
        current = gate.fingerprint(environment)
        if current == previous:
            return current
        previous = current
        if attempt + 1 < attempts:
            sleep(interval_s)
    raise ProposalError(f"{environment} did not settle; try again in a minute")


def build_proposal(investigation_id: str, candidate: RepairCandidate, ops: list[WriteOp],
                   gate: Any, *, environment: str, explanation: str,
                   now: datetime | None = None, proposal_id: str | None = None,
                   sleep: Callable[[float], None] = time.sleep,
                   ) -> tuple[Proposal, list[dict[str, Any]]]:
    if not ops:
        raise ProposalError("no ops to propose")
    now = now or datetime.now(UTC)
    proposal_id = proposal_id or f"prop_{secrets.token_hex(6)}"
    fingerprint = settled_fingerprint(gate, environment, sleep=sleep)
    calls = gate.render(ops, proposal_id)
    proposal = Proposal(id=proposal_id, investigation_id=investigation_id,
                        candidate=candidate.model_copy(update={"ops": ops}),
                        payload_hash=payload_hash(ops), fingerprint=fingerprint,
                        explanation=explanation, state=ProposalState.PROPOSED,
                        created_at=now, expires_at=now + PROPOSAL_TTL)
    return proposal, calls


def expectations(ops: Sequence[WriteOp], member: Identity, control: Identity,
                 release_values: Mapping[str, Any]) -> list[Expectation]:
    """What each identity should be served once the ops are live.

    member: the incident's identity (inside the segment); control: one outside it.
    """
    inside: dict[str, Any] = {}
    outside: dict[str, Any] = {}
    for op in ops:
        if op.tool == "create_segment_override":
            inside[op.feature] = op.value
            outside[op.feature] = release_values[op.feature]
        elif op.tool == "update_feature_state":
            inside[op.feature] = outside[op.feature] = op.value
        else:  # delete_feature_segment: both fall back to the environment default
            inside[op.feature] = outside[op.feature] = release_values[op.feature]
    return [Expectation(member, inside), Expectation(control, outside)]
