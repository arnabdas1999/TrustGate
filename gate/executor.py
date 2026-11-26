"""Runs one approved write. Nothing reaches Flagsmith without passing every step, in order:

1. approvals.check   signature, expiry, payload hash, scope (refusal: nothing sent)
2. writer.render     ops -> the exact v2 calls (refusal: nothing sent, approval kept)
3. stale check       a fresh edge fingerprint must equal the one approved (else STALE, 409;
                     the approval is kept but the proposal must be re-investigated)
4. approvals.consume marks the approval used, atomically (a second request gets REUSED)
5. writer.send       sends only the rendered calls, once each, never retried
6. audit             one append-only entry with approver, hashes, fingerprints and result

Steps 3 and 5 are not atomic: a change landing between them can slip through. Flagsmith has
no conditional write, so this is stale-state detection, not atomic execution; the after
fingerprint and the verifier's read-back catch an unexpected difference.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from contracts.models import Approval, WriteOp
from gate.approvals import ApprovalRefused, ApprovalStore, Refusal
from gate.audit_log import AuditLog
from gate.scope import GateScope
from gate.writer import IdResolver, McpCall, WriterError, WriteResult, render, send


class StaleState(Exception):
    """Flagsmith changed since the proposal (or its state could not be checked)."""


@dataclass
class ExecutionResult:
    proposal_id: str
    approval_id: str
    status: str  # "applied" or "failed"
    calls: list[McpCall]
    results: list[WriteResult] = field(default_factory=list)
    fingerprint_before: str | None = None
    fingerprint_after: str | None = None
    after_settled: bool = False  # True once the edge document showed the write

    @property
    def version_uuids(self) -> list[str]:
        return [r.version_uuid for r in self.results if r.ok and r.version_uuid]

    def to_dict(self) -> dict[str, Any]:
        return {"proposal_id": self.proposal_id, "approval_id": self.approval_id,
                "status": self.status, "calls": [c.to_dict() for c in self.calls],
                "results": [{"ok": r.ok, "detail": r.detail, "version_uuid": r.version_uuid}
                            for r in self.results],
                "fingerprint_before": self.fingerprint_before,
                "fingerprint_after": self.fingerprint_after,
                "after_settled": self.after_settled}


class WriteFailed(Exception):
    def __init__(self, result: ExecutionResult):
        detail = next((r.detail for r in result.results if not r.ok), "unknown")
        super().__init__(f"write failed: {detail}")
        self.result = result


class Executor:
    def __init__(self, *, approvals: ApprovalStore, scope: GateScope, ids: IdResolver, mcp: Any,
                 fingerprint: Callable[[str], str], audit: AuditLog, settle_reads: int = 15,
                 settle_interval_s: float = 2.0, sleep: Callable[[float], None] = time.sleep):
        self._approvals = approvals
        self._scope = scope
        self._ids = ids
        self._mcp = mcp
        self._fingerprint = fingerprint
        self._audit = audit
        self._settle_reads = settle_reads
        self._settle_interval_s = settle_interval_s
        self._sleep = sleep

    def _after_fingerprint(self, environment: str, before: str) -> tuple[str | None, bool]:
        """The edge document lags a write by a few seconds (seen Nov 26). Read until it moves."""
        after = None
        for attempt in range(self._settle_reads):
            try:
                after = self._fingerprint(environment)
            except Exception:  # noqa: BLE001 (the write already happened; report, don't hide it)
                return None, False
            if after != before:
                return after, True
            if attempt + 1 < self._settle_reads:
                self._sleep(self._settle_interval_s)
        return after, False

    def _refuse(self, approval: Approval, reason: str) -> None:
        self._audit.append(actor="executor", action="refused", tool=None, args={},
                           result=reason, proposal_id=approval.proposal_id,
                           approval_id=approval.approval_id, approver=approval.approver)

    def execute(self, approval: Approval, ops: Sequence[WriteOp],
                approved_fingerprint: str) -> ExecutionResult:
        try:
            self._approvals.check(approval, ops)
        except ApprovalRefused as exc:
            self._refuse(approval, str(exc))
            raise

        try:
            calls = render(ops, self._scope, self._ids, proposal_id=approval.proposal_id,
                           approval_id=approval.approval_id)
        except WriterError as exc:
            self._refuse(approval, f"bad_request: {exc}")
            raise ApprovalRefused(Refusal.BAD_REQUEST, str(exc)) from None

        environment = ops[0].environment
        try:
            before = self._fingerprint(environment)
        except Exception as exc:  # noqa: BLE001
            self._refuse(approval, f"stale: could not fingerprint {environment}")
            raise StaleState(f"could not fingerprint {environment}: "
                             f"{type(exc).__name__}") from None
        if before != approved_fingerprint:
            self._refuse(approval, "stale: Flagsmith changed since the proposal")
            raise StaleState("Flagsmith changed since the proposal")

        try:
            used = self._approvals.consume(approval, ops)
        except ApprovalRefused as exc:
            self._refuse(approval, str(exc))
            raise

        results = send(calls, self._mcp)
        ok = bool(results) and all(r.ok for r in results) and len(results) == len(calls)
        # Only wait for the edge to move if something was written.
        after, settled = self._after_fingerprint(environment, before) if ok else (None, False)
        result = ExecutionResult(approval.proposal_id, approval.approval_id,
                                 "applied" if ok else "failed", calls, results, before, after,
                                 settled)
        failure = next((r.detail for r in results if not r.ok), "")
        self._audit.append(
            actor="executor", action="write", tool=calls[0].tool,
            args={"calls": [c.to_dict() for c in calls]},
            result="applied" if ok else f"failed: {failure}"[:500],
            proposal_id=approval.proposal_id, approval_id=approval.approval_id,
            approver=used.approver, payload_hash=used.payload_hash,
            fingerprint_before=before, fingerprint_after=after, after_settled=settled,
            version_uuids=result.version_uuids)
        if not ok:
            raise WriteFailed(result)
        return result
