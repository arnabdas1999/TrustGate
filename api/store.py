"""In-memory store for the skeleton. The orchestrator (Naman) replaces it with SQLite."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from api import settings
from api.schemas import InvestigationView
from contracts import (
    ActivityPing,
    Approval,
    AuditEntry,
    Incident,
    Investigation,
    Proposal,
)


@dataclass
class Session:
    token: str
    approver: str
    expires_at: datetime


@dataclass
class Store:
    incidents: dict[str, Incident] = field(default_factory=dict)
    investigations: dict[str, Investigation] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    approvals: dict[str, Approval] = field(default_factory=dict)
    # The exact MCP call(s) the gate rendered for each proposal, shown on the proposal card.
    proposal_calls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    audit: list[AuditEntry] = field(default_factory=list)
    activity: list[ActivityPing] = field(default_factory=list)
    sessions: dict[str, Session] = field(default_factory=dict)

    # --- sessions ---------------------------------------------------------------------------
    def new_session(self, approver: str) -> Session:
        session = Session(
            token=secrets.token_urlsafe(32),
            approver=approver,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.SESSION_HOURS),
        )
        self.sessions[session.token] = session
        return session

    def session_for(self, token: str) -> Session | None:
        session = self.sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= datetime.now(UTC):
            self.sessions.pop(token, None)
            return None
        return session

    # --- activity (human-attention metric) --------------------------------------------------
    def active_ms(self, investigation_id: str) -> int:
        return sum(p.active_ms for p in self.activity if p.investigation_id == investigation_id)


# Real investigations exported from a live run (GET /api/investigations/<id> saved as JSON).
EXPORTS_DIR = Path(__file__).resolve().parents[1] / "bench" / "exports"


def load_exports(directory: Path) -> list[InvestigationView]:
    """Every exported investigation in `directory`, so mock mode shows judges measured data next
    to the fixtures. A missing directory means none."""
    if not directory.is_dir():
        return []
    return [InvestigationView.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))]


def seeded(mock: bool, exports_dir: Path | None = None) -> Store:
    store = Store()
    if mock:
        from api import mock_data

        store.investigations, store.proposals, store.audit = mock_data.build()
        store.proposal_calls = mock_data.calls(store.proposals)
        for view in load_exports(exports_dir if exports_dir is not None else EXPORTS_DIR):
            store.investigations[view.investigation.id] = view.investigation
            if view.proposal is not None:
                store.proposals[view.proposal.id] = view.proposal
                store.proposal_calls[view.proposal.id] = view.calls
    return store
