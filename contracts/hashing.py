"""Canonical hashing shared by the API (signs approvals) and the gate (checks them)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from typing import Any

from contracts.models import WriteOp


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, so equal data always hashes the same."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(ops: Iterable[WriteOp]) -> str:
    """SHA-256 over the exact tool names and arguments of the approved writes, in order."""
    body = [op.model_dump(mode="json") for op in ops]
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def sign_approval(
    secret: str, approval_id: str, proposal_id: str, payload: str, expires_at: str
) -> str:
    """HMAC-SHA256 the API attaches to an approval. The gate recomputes it with the same secret."""
    if not secret:
        raise ValueError("APPROVAL_SIGNING_SECRET is not set")
    message = canonical_json(
        {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "payload_hash": payload,
            "expires_at": expires_at,
        }
    )
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(
    secret: str, signature: str, approval_id: str, proposal_id: str, payload: str, expires_at: str
) -> bool:
    expected = sign_approval(secret, approval_id, proposal_id, payload, expires_at)
    # Compare bytes: compare_digest raises TypeError on non-ASCII str input, which would turn
    # a forged signature into an unaudited 500 instead of a refusal.
    return hmac.compare_digest(expected.encode("utf-8"), str(signature).encode("utf-8"))
