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

import hmac
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from contracts.models import Approval, WriteOp
from gate.approvals import ApprovalRefused, ApprovalStore, Refusal
from gate.audit_log import AuditLog
from gate.mcp_client import McpError
from gate.scope import GateScope
from gate.writer import IdResolver, McpCall, WriterError, WriteResult, render, send

# Per-process state: this lock and the approvals store's written-over check only serialise
# writes inside ONE gate process. Run the gate as a single uvicorn worker (the compose command
# does); several workers would each have their own lock.
_ENV_LOCKS: dict[str, threading.Lock] = {}
_ENV_LOCKS_GUARD = threading.Lock()


def _environment_lock(environment: str) -> threading.Lock:
    """One write at a time per environment, from the stale check to the settled fingerprint."""
    with _ENV_LOCKS_GUARD:
        return _ENV_LOCKS.setdefault(environment, threading.Lock())


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
                 fingerprint: Callable[[str], str], audit: AuditLog, settle_reads: int = 8,
                 settle_interval_s: float = 1.0, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 settle_budget_s: float = 10.0):
        self._approvals = approvals
        self._scope = scope
        self._ids = ids
        self._mcp = mcp
        self._fingerprint = fingerprint
        self._audit = audit
        # 8 x 1 s keeps a write's response well inside the API's 30 s timeout; the verifier
        # does the longer read-back afterwards.
        self._settle_reads = settle_reads
        self._settle_interval_s = settle_interval_s
        self._sleep = sleep
        self._monotonic = monotonic
        # Wall-clock cap on the settle reads (each edge read has its own short timeout), so a
        # write answers inside the API's and GateClient's 30 s timeouts.
        self._settle_budget_s = settle_budget_s

    def _after_fingerprint(self, environment: str, before: str) -> tuple[str | None, bool]:
        """The edge document lags a write by a few seconds (seen Nov 26). Read until it moves,
        within settle_reads and the settle time budget."""
        after = None
        deadline = self._monotonic() + self._settle_budget_s
        for attempt in range(self._settle_reads):
            try:
                after = self._fingerprint(environment)
            except Exception:  # noqa: BLE001 (the write already happened; report, don't hide it)
                return None, False
            if after != before:
                return after, True
            if attempt + 1 >= self._settle_reads \
                    or self._monotonic() + self._settle_interval_s >= deadline:
                break
            self._sleep(self._settle_interval_s)
        return after, False

    def _refuse(self, approval: Approval, reason: str) -> None:
        self._audit.append(actor="executor", action="refused", tool=None, args={},
                           result=reason, proposal_id=approval.proposal_id,
                           approval_id=approval.approval_id, approver_claimed=approval.approver,
                           approver_verified=False)

    def _refused(self, approval: Approval, reason: Refusal, detail: str) -> ApprovalRefused:
        self._refuse(approval, f"{reason}: {detail}")
        return ApprovalRefused(reason, detail)

    def execute(self, approval: Approval, ops: Sequence[WriteOp],
                request_fingerprint: str | None = None) -> ExecutionResult:
        """Run one approved write. The stale check compares against the fingerprint the gate
        itself recorded when the proposal was registered; the request's copy only has to agree."""
        try:
            self._approvals.check(approval, ops)
        except ApprovalRefused as exc:
            self._refuse(approval, str(exc))
            raise

        reg = self._approvals.registration(approval.proposal_id)
        if reg is None:
            raise self._refused(approval, Refusal.BAD_REQUEST,
                                "proposal was not registered with the gate")
        if not hmac.compare_digest(reg.payload_hash, approval.payload_hash):
            raise self._refused(approval, Refusal.PAYLOAD_MISMATCH,
                                "approved payload differs from the registered proposal")
        environment = ops[0].environment
        if reg.environment != environment:
            raise self._refused(approval, Refusal.OUT_OF_SCOPE,
                                f"proposal was registered for {reg.environment}")
        if request_fingerprint is not None and request_fingerprint != reg.fingerprint:
            raise self._refused(approval, Refusal.BAD_REQUEST,
                                "request fingerprint differs from the registered one")

        try:
            calls = render(ops, self._scope, self._ids, proposal_id=approval.proposal_id,
                           approval_id=approval.approval_id)
        except (WriterError, McpError, KeyError, ValueError, TypeError) as exc:
            raise self._refused(approval, Refusal.BAD_REQUEST,
                                f"could not render the write: {type(exc).__name__}: {exc}") \
                from None

        with _environment_lock(environment):
            if self._approvals.was_written_over(environment, reg.fingerprint):
                self._refuse(approval, "stale: the gate already wrote over this state")
                raise StaleState("Flagsmith was written since the proposal")
            try:
                before = self._fingerprint(environment)
            except Exception as exc:  # noqa: BLE001
                self._refuse(approval, f"stale: could not fingerprint {environment}")
                raise StaleState(f"could not fingerprint {environment}: "
                                 f"{type(exc).__name__}") from None
            if before != reg.fingerprint:
                self._refuse(approval, "stale: Flagsmith changed since the proposal")
                raise StaleState("Flagsmith changed since the proposal")

            # A dead MCP session must fail here, before the approval is used up.
            ensure_session = getattr(self._mcp, "ensure_session", None)
            if ensure_session is not None:
                try:
                    ensure_session()
                except Exception as exc:  # noqa: BLE001 (any failure here must not burn it)
                    self._refuse(approval, f"failed: MCP session unavailable: {exc}")
                    failed = ExecutionResult(approval.proposal_id, approval.approval_id,
                                             "failed", calls,
                                             [WriteResult(calls[0], ok=False,
                                                          detail=f"MCP session: {exc}")], before)
                    raise WriteFailed(failed) from None

            try:
                used = self._approvals.consume(approval, ops)
            except ApprovalRefused as exc:
                self._refuse(approval, str(exc))
                raise
            except Exception as exc:  # noqa: BLE001 (e.g. a database error): audited, no write
                raise self._refused(approval, Refusal.BAD_REQUEST,
                                    f"could not record the approval: {type(exc).__name__}") \
                    from None

            results = send(calls, self._mcp)
            ok = bool(results) and all(r.ok for r in results) and len(results) == len(calls)
            after, settled = None, False
            try:
                # Any attempted call may have landed (a timeout is ambiguous), so the starting
                # state counts as written over and gets a settle read whenever one was sent.
                if results:
                    self._approvals.mark_written_over(environment, before)
                    after, settled = self._after_fingerprint(environment, before)
            finally:
                # Always audit what was sent, even if recording the written-over state failed.
                failure = next((r.detail for r in results if not r.ok), "")
                version_uuids = [r.version_uuid for r in results if r.ok and r.version_uuid]
                self._audit.append(
                    actor="executor", action="write", tool=calls[0].tool,
                    args={"calls": [c.to_dict() for c in calls]},
                    result="applied" if ok else f"failed: {failure}"[:500],
                    proposal_id=approval.proposal_id, approval_id=approval.approval_id,
                    approver_claimed=used.approver_claimed, approver_verified=False,
                    payload_hash=used.payload_hash, fingerprint_before=before,
                    fingerprint_after=after, after_settled=settled, version_uuids=version_uuids)

        result = ExecutionResult(approval.proposal_id, approval.approval_id,
                                 "applied" if ok else "failed", calls, results, before, after,
                                 settled)
        if not ok:
            raise WriteFailed(result)
        return result
