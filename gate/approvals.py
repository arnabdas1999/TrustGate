"""Approval checks the gate runs before any Flagsmith write.

An approval is accepted once, and only if all of these hold:
1. It is well formed: at least one op, a timezone-aware expiry.
2. The HMAC from the API verifies (contracts.hashing). Checked in `_signature_valid` only.
3. It has not expired by the gate's own clock, and its expiry is no more than 15 minutes
   ahead of that clock (a long-lived approval is refused even if signed).
4. The payload hash the gate recomputes from the ops it received equals the signed one.
5. Every op stays in the gate's scope: our project and an allowlisted environment.
6. Its approval_id has never been used. The gate keeps its own SQLite table with a unique
   approval_id and never trusts `used_at` from the request. Marking it used is the last step
   and happens atomically, so a refused request does not burn a genuine approval.

The stale-state (fingerprint) check is the executor's job, right before the write.
"""

from __future__ import annotations

import hmac
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from contracts.hashing import payload_hash, verify_signature
from contracts.models import Approval, WriteOp
from gate.scope import GateScope

MAX_APPROVAL_LIFETIME = timedelta(minutes=15)
# The edge document lags a write by seconds (seen Nov 26); refuse the replaced state this long.
WRITTEN_OVER_WINDOW = timedelta(minutes=2)


class Refusal(StrEnum):
    BAD_REQUEST = "bad_request"
    FORGED = "forged"
    EXPIRED = "expired"
    EXPIRY_TOO_FAR = "expiry_too_far"
    PAYLOAD_MISMATCH = "payload_mismatch"
    OUT_OF_SCOPE = "out_of_scope"
    REUSED = "reused"


class RegistrationConflict(Exception):
    """The proposal id is already registered with a different payload, fingerprint or env."""


class ApprovalRefused(Exception):
    def __init__(self, reason: Refusal, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class UsedApproval:
    approval_id: str
    proposal_id: str
    # The approver isn't covered by the API's signature yet (contracts fix pending), so the
    # gate records it as a claim, never as verified identity.
    approver_claimed: str
    payload_hash: str
    used_at: datetime


@dataclass(frozen=True)
class Registration:
    """What the gate itself recorded when the proposal was registered (POST /gate/register)."""
    proposal_id: str
    payload_hash: str
    fingerprint: str
    environment: str


def _signature_valid(secret: str, approval: Approval) -> bool:
    """The one place the gate checks the API's HMAC.

    contracts.hashing currently signs `expires_at.isoformat()` of a UTC datetime ("+00:00"),
    while the JSON wire format says "Z". So we format the parsed datetime the way the API
    signed it. When contracts.hashing takes a datetime itself, pass approval.expires_at here.
    """
    expires = approval.expires_at.astimezone(UTC).isoformat()
    return verify_signature(secret, approval.signature, approval.approval_id,
                            approval.proposal_id, approval.payload_hash, expires)


class ApprovalStore:
    def __init__(self, db_path: str | Path, *, secret: str, scope: GateScope,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        if not secret:
            raise ValueError("APPROVAL_SIGNING_SECRET is not set")
        self._secret = secret
        self._scope = scope
        self._clock = clock
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._conn:
            # One approval per approval_id AND per proposal: a proposal is written at most once.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS used_approvals_v2 ("
                " approval_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,"
                " approver_claimed TEXT NOT NULL, approver_verified INTEGER NOT NULL DEFAULT 0,"
                " payload_hash TEXT NOT NULL, used_at TEXT NOT NULL)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS registered_proposals ("
                " proposal_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,"
                " fingerprint TEXT NOT NULL, environment TEXT NOT NULL, registered_at TEXT)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS written_over ("
                " environment TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, at TEXT NOT NULL)")

    # -- proposals registered by the gate itself ---------------------------------------------

    def register_proposal(self, proposal_id: str, payload_hash: str, fingerprint: str,
                          environment: str) -> Registration:
        reg = Registration(proposal_id, payload_hash, fingerprint, environment)
        with self._lock:  # check and insert as one step: no race between two registrations
            row = self._conn.execute(
                "SELECT proposal_id, payload_hash, fingerprint, environment "
                "FROM registered_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if row is not None:
                if Registration(*row) != reg:
                    raise RegistrationConflict(
                        f"proposal {proposal_id} is already registered differently")
                return reg
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO registered_proposals VALUES (?, ?, ?, ?, ?)",
                        (proposal_id, payload_hash, fingerprint, environment,
                         self._clock().isoformat()))
            except sqlite3.IntegrityError:  # another process got there first
                raise RegistrationConflict(
                    f"proposal {proposal_id} is already registered") from None
        return reg

    def registration(self, proposal_id: str) -> Registration | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT proposal_id, payload_hash, fingerprint, environment "
                "FROM registered_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        return Registration(*row) if row else None

    # -- the state the last write replaced, per environment ----------------------------------

    def mark_written_over(self, environment: str, fingerprint: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO written_over VALUES (?, ?, ?)",
                               (environment, fingerprint, self._clock().isoformat()))

    def was_written_over(self, environment: str, fingerprint: str) -> bool:
        """True if the gate's last write here started from `fingerprint`, within the edge-lag
        window. After that, an identical fingerprint is a real state again (e.g. a revert)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint, at FROM written_over WHERE environment = ?",
                (environment,)).fetchone()
        if row is None or row[0] != fingerprint:
            return False
        return self._clock() - datetime.fromisoformat(row[1]) < WRITTEN_OVER_WINDOW

    def is_used(self, approval_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM used_approvals_v2 WHERE approval_id = ?",
                                     (approval_id,)).fetchone()
        return row is not None

    def proposal_used(self, proposal_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM used_approvals_v2 WHERE proposal_id = ?",
                                     (proposal_id,)).fetchone()
        return row is not None

    def check(self, approval: Approval, ops: Sequence[WriteOp]) -> None:
        """Every rule except single use. Raises ApprovalRefused."""
        if not ops:
            raise ApprovalRefused(Refusal.BAD_REQUEST, "no ops to write")
        if approval.expires_at.tzinfo is None:
            raise ApprovalRefused(Refusal.BAD_REQUEST, "expires_at has no timezone")

        if not _signature_valid(self._secret, approval):
            raise ApprovalRefused(Refusal.FORGED, "signature does not verify")

        now = self._clock()
        if now >= approval.expires_at:
            raise ApprovalRefused(Refusal.EXPIRED, f"expired at {approval.expires_at.isoformat()}")
        if approval.expires_at > now + MAX_APPROVAL_LIFETIME:
            raise ApprovalRefused(Refusal.EXPIRY_TOO_FAR,
                                  "expiry is more than 15 minutes ahead of the gate's clock")

        if not hmac.compare_digest(payload_hash(ops), approval.payload_hash):
            raise ApprovalRefused(Refusal.PAYLOAD_MISMATCH,
                                  "ops differ from the approved payload")

        for op in ops:
            if not self._scope.allows_project(op.project):
                raise ApprovalRefused(Refusal.OUT_OF_SCOPE, f"project {op.project!r}")
            if op.environment not in self._scope.environments:
                raise ApprovalRefused(Refusal.OUT_OF_SCOPE, f"environment {op.environment!r}")

    def consume(self, approval: Approval, ops: Sequence[WriteOp]) -> UsedApproval:
        """Check everything, then mark the approval used. Call right before the write."""
        self.check(approval, ops)
        record = UsedApproval(approval.approval_id, approval.proposal_id, approval.approver,
                              approval.payload_hash, self._clock())
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO used_approvals_v2 VALUES (?, ?, ?, 0, ?, ?)",
                    (record.approval_id, record.proposal_id, record.approver_claimed,
                     record.payload_hash, record.used_at.isoformat()))
        except sqlite3.IntegrityError:
            raise ApprovalRefused(Refusal.REUSED, f"approval {approval.approval_id} or proposal "
                                  f"{approval.proposal_id} was already used") from None
        return record
