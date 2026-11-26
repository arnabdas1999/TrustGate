"""Response shapes that exist only at the API boundary.

The UI mirrors them in web/src/lib/types.ts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from contracts import Investigation, Proposal


class InvestigationView(BaseModel):
    investigation: Investigation
    proposal: Proposal | None = None
    # The exact MCP call(s) the gate rendered for the proposal: what Flagsmith will receive.
    calls: list[dict[str, Any]] = []
    # Set when the proposal is a rollback: the id of the repair it undoes.
    rollback_of: str | None = None
    active_ms: int = 0  # human attention logged by the UI for this investigation


class Health(BaseModel):
    status: str
    mock: bool
    gate_url: str


class ActivityTotal(BaseModel):
    investigation_id: str
    active_ms: int
