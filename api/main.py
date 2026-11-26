"""TrustGate API.

CP0 skeleton: every endpoint from the Development plan exists. With MOCK=1 it serves fixture
data so the UI can be built now; the orchestrator logic (Naman) replaces the mock paths.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from api import settings
from api.schemas import ActivityTotal, Health, InvestigationView
from api.store import Session, Store, seeded
from contracts import (
    ActivityPing,
    Approval,
    AuditEntry,
    Incident,
    Investigation,
    InvestigationStatus,
    Proposal,
    ProposalState,
    ReadBack,
    SessionRequest,
    SessionResponse,
    StartInvestigationRequest,
    Timestamps,
    sign_approval,
)
from orchestrator.gate_client import GateClient, GateError
from orchestrator.wiring import start_in_background, verify_in_background


def create_app(mock: bool | None = None) -> FastAPI:
    is_mock = settings.mock_mode() if mock is None else mock
    app = FastAPI(title="TrustGate API", version="0.1.0")
    app.state.store = seeded(is_mock)
    app.state.mock = is_mock

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.web_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def store() -> Store:
        return app.state.store

    def require_session(
        authorization: str = Header(default=""), db: Store = Depends(store)
    ) -> Session:
        scheme, _, token = authorization.partition(" ")
        session = db.session_for(token) if scheme.lower() == "bearer" and token else None
        if session is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "approver login required")
        return session

    # --- health ---------------------------------------------------------------------------
    @app.get("/api/health", response_model=Health)
    def health() -> Health:
        return Health(status="ok", mock=app.state.mock, gate_url=settings.gate_url())

    # --- approver session -----------------------------------------------------------------
    @app.post("/api/session", response_model=SessionResponse)
    def login(body: SessionRequest, db: Store = Depends(store)) -> SessionResponse:
        expected = settings.approver_password()
        if not expected:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "login disabled: APPROVER_PASSWORD is not set")
        if not hmac.compare_digest(body.password.encode(), expected.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")
        session = db.new_session(settings.approver_name())
        return SessionResponse(token=session.token, approver=session.approver,
                               expires_at=session.expires_at)

    # --- incidents and investigations -----------------------------------------------------
    @app.post("/api/incidents", response_model=Incident, status_code=201)
    def capture_incident(incident: Incident, db: Store = Depends(store)) -> Incident:
        db.incidents[incident.id] = incident
        return incident

    @app.post("/api/investigations", response_model=Investigation, status_code=201)
    def start_investigation(body: StartInvestigationRequest,
                            db: Store = Depends(store)) -> Investigation:
        incident = db.incidents.get(body.incident_id)
        if incident is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown incident")
        investigation = Investigation(
            id=f"inv_{secrets.token_hex(6)}", incident_id=incident.id, domain=incident.domain,
            mode=body.mode, status=InvestigationStatus.CAPTURING,
            timestamps=Timestamps(alert=datetime.now(UTC)),
        )
        db.investigations[investigation.id] = investigation
        if not app.state.mock:  # the orchestrator moves the status forward in the background
            start_in_background(db, investigation.id)
        return investigation

    @app.get("/api/investigations", response_model=list[Investigation])
    def list_investigations(db: Store = Depends(store)) -> list[Investigation]:
        return sorted(db.investigations.values(), key=lambda i: i.timestamps.alert, reverse=True)

    @app.get("/api/investigations/{investigation_id}", response_model=InvestigationView)
    def get_investigation(investigation_id: str, db: Store = Depends(store)) -> InvestigationView:
        investigation = db.investigations.get(investigation_id)
        if investigation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown investigation")
        proposal = db.proposals.get(investigation.proposal_id or "")
        if proposal is not None:
            _expire_if_due(db, proposal)
        return InvestigationView(investigation=investigation, proposal=proposal,
                                 active_ms=db.active_ms(investigation_id))

    # --- proposals ------------------------------------------------------------------------
    @app.get("/api/proposals/{proposal_id}", response_model=Proposal)
    def get_proposal(proposal_id: str, db: Store = Depends(store)) -> Proposal:
        proposal = _proposal_or_404(db, proposal_id)
        _expire_if_due(db, proposal)
        return proposal

    @app.post("/api/proposals/{proposal_id}/approve", response_model=Proposal)
    def approve(proposal_id: str, db: Store = Depends(store),
                session: Session = Depends(require_session)) -> Proposal:
        proposal = _proposal_or_404(db, proposal_id)
        _expire_if_due(db, proposal)
        if proposal.state != ProposalState.PROPOSED:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"proposal is {proposal.state.value}, not proposed")
        now = datetime.now(UTC)
        investigation = db.investigations[proposal.investigation_id]
        db.audit.append(AuditEntry(ts=now, actor=session.approver, action="approve",
                                   proposal_id=proposal.id,
                                   result=f"payload {proposal.payload_hash[:12]}"))
        if app.state.mock:
            _mock_apply(db, proposal, investigation, now)
        else:
            _send_to_gate(db, proposal, investigation, session, now)
        return proposal

    @app.post("/api/proposals/{proposal_id}/reject", response_model=Proposal)
    def reject(proposal_id: str, db: Store = Depends(store),
               session: Session = Depends(require_session)) -> Proposal:
        proposal = _proposal_or_404(db, proposal_id)
        if proposal.state != ProposalState.PROPOSED:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"proposal is {proposal.state.value}, not proposed")
        proposal.state = ProposalState.REJECTED
        db.investigations[proposal.investigation_id].status = InvestigationStatus.REJECTED
        db.audit.append(AuditEntry(ts=datetime.now(UTC), actor=session.approver, action="reject",
                                   proposal_id=proposal.id))
        return proposal

    # --- audit trail and activity ---------------------------------------------------------
    @app.get("/api/audit", response_model=list[AuditEntry])
    def audit(db: Store = Depends(store)) -> list[AuditEntry]:
        # The gate's append-only trail (reads, refusals, writes) plus the API's own approve and
        # reject entries. MOCK=1 keeps serving the in-memory fixtures only.
        entries = list(db.audit)
        if not app.state.mock:
            try:
                entries += GateClient(settings.gate_url()).audit_entries()
            except GateError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        return sorted(entries, key=lambda e: e.ts)

    @app.post("/api/activity", status_code=204)
    def record_activity(ping: ActivityPing, db: Store = Depends(store)) -> None:
        db.activity.append(ping)

    @app.get("/api/activity/{investigation_id}", response_model=ActivityTotal)
    def activity_total(investigation_id: str, db: Store = Depends(store)) -> ActivityTotal:
        return ActivityTotal(investigation_id=investigation_id,
                             active_ms=db.active_ms(investigation_id))

    return app


def _proposal_or_404(db: Store, proposal_id: str) -> Proposal:
    proposal = db.proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown proposal")
    return proposal


def _expire_if_due(db: Store, proposal: Proposal) -> None:
    if proposal.state == ProposalState.PROPOSED and proposal.expires_at <= datetime.now(UTC):
        proposal.state = ProposalState.EXPIRED
        db.investigations[proposal.investigation_id].status = InvestigationStatus.EXPIRED


def _mock_apply(db: Store, proposal: Proposal, investigation: Investigation,
                now: datetime) -> None:
    """Pretend the gate wrote the override and the verifier read it back."""
    proposal.state = ProposalState.VERIFIED
    proposal.readback = [
        ReadBack(identity={"identifier": "payments-prod", "traits": {"env": "prod", "tier": 0}},
                 values={"agent_prompt_version": "v1_strict"}),
        ReadBack(identity={"identifier": "staging-api", "traits": {"env": "staging", "tier": 2}},
                 values={"agent_prompt_version": "v2_fast"}),
    ]
    investigation.status = InvestigationStatus.VERIFIED
    investigation.timestamps.approved = now
    investigation.timestamps.verified = now + timedelta(seconds=19)
    db.audit.append(AuditEntry(ts=now, actor="executor", action="write",
                               tool=proposal.candidate.ops[0].tool,
                               args=proposal.candidate.ops[0].model_dump(mode="json"),
                               proposal_id=proposal.id, result="applied (mock)"))


def _send_to_gate(db: Store, proposal: Proposal, investigation: Investigation, session: Session,
                  now: datetime) -> None:
    """Sign a single-use approval and hand it to the gate. The gate re-checks everything."""
    secret = settings.signing_secret()
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "APPROVAL_SIGNING_SECRET is not set")
    expires_at = min(proposal.expires_at, now + timedelta(minutes=settings.APPROVAL_MINUTES))
    approval_id = f"appr_{secrets.token_hex(8)}"
    approval = Approval(
        proposal_id=proposal.id, approval_id=approval_id, approver=session.approver,
        payload_hash=proposal.payload_hash,
        signature=sign_approval(secret, approval_id, proposal.id, proposal.payload_hash,
                                expires_at.isoformat()),
        created_at=now, expires_at=expires_at,
    )
    db.approvals[approval_id] = approval
    try:
        response = httpx.post(
            f"{settings.gate_url()}/gate/write",
            json={"approval": approval.model_dump(mode="json"),
                  "ops": [op.model_dump(mode="json") for op in proposal.candidate.ops],
                  "fingerprint": proposal.fingerprint},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"gate unreachable: {exc}") from exc
    if response.status_code == status.HTTP_409_CONFLICT:
        proposal.state = ProposalState.STALE
        investigation.status = InvestigationStatus.STALE
        raise HTTPException(status.HTTP_409_CONFLICT, "Flagsmith changed since the proposal")
    if response.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"gate refused the write ({response.status_code}): {response.text}")
    proposal.state = ProposalState.APPLIED
    investigation.status = InvestigationStatus.APPLIED
    investigation.timestamps.approved = now
    # Read-back in the background sets VERIFIED, or FAILED plus a rollback proposal.
    # (Fresh post-write runs join once search/ and sandbox/ land.)
    verify_in_background(db, investigation.id)


app = create_app()
