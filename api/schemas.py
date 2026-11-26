"""Response shapes that exist only at the API boundary.

The UI mirrors them in web/src/lib/types.ts.
"""

from __future__ import annotations

from pydantic import BaseModel

from contracts import Investigation, Proposal


class InvestigationView(BaseModel):
    investigation: Investigation
    proposal: Proposal | None = None
    active_ms: int = 0  # human attention logged by the UI for this investigation


class Health(BaseModel):
    status: str
    mock: bool
    gate_url: str


class ActivityTotal(BaseModel):
    investigation_id: str
    active_ms: int
