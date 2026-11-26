"""TrustGate gate service (port 8100, internal to the Docker network only).

The only process that holds the Flagsmith organisation key. Endpoints:
- POST /gate/read         allowlisted reads: {tool, args}. Default deny, audited.
- POST /gate/write        {approval, ops, fingerprint}, exactly as api.main._send_to_gate posts.
                          200 applied, 403 refused (reason), 409 stale, 502 write failed.
- GET  /gate/fingerprint  ?environment=NAME: canonical fingerprint of the edge document.
- POST /gate/render       {ops, proposal_id}: the exact MCP call(s) a write would send.
- POST /gate/register     {ops, proposal_id}: record the proposal's payload hash and the gate's
                          OWN settled fingerprint. /gate/write compares against this record,
                          never against a fingerprint the caller sends.
- GET  /gate/audit        the gate's audit trail (not itself logged, so UI polling doesn't
                          flood the log)
- GET  /health

Configuration comes only from the gate's environment (compose passes .env.gate):
FLAGSMITH_API_KEY, APPROVAL_SIGNING_SECRET, GATE_PROJECT_ID, GATE_PROJECT_NAME,
GATE_ENVIRONMENTS ("Development:121197,Demo Production:121198"),
GATE_SERVER_KEY_<ENV> (a ser. key per environment, e.g. GATE_SERVER_KEY_DEMO_PRODUCTION),
optional FLAGSMITH_MCP_URL, GATE_DB_PATH, GATE_AUDIT_PATH, and GATE_SHARED_TOKEN: when set,
every /gate/* call needs "Authorization: Bearer <token>" (off by default until the API sends
it). Without the required settings the service still
starts (so `docker compose up` works) and answers 503.
"""

from __future__ import annotations

import hmac
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from contracts.hashing import payload_hash
from contracts.models import Approval, WriteOp
from gate.allowlist import ReadKind, ToolDenied, check_read
from gate.approvals import ApprovalRefused, ApprovalStore, RegistrationConflict
from gate.audit_log import AuditLog, merge_versions
from gate.executor import Executor, StaleState, WriteFailed
from gate.fingerprint import FingerprintError, canonical_view, fetch_environment_document
from gate.fingerprint import fingerprint as fingerprint_of
from gate.mcp_client import DEFAULT_MCP_URL, McpClient, McpError
from gate.scope import GateScope
from gate.writer import IdResolver, McpIdResolver, WriterError, render


def _env_key(name: str) -> str:
    return "GATE_SERVER_KEY_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


@dataclass
class GateSettings:
    scope: GateScope
    api_key: str = field(default="", repr=False)
    signing_secret: str = field(default="", repr=False)
    server_keys: dict[str, str] = field(default_factory=dict, repr=False)
    mcp_url: str = DEFAULT_MCP_URL
    db_path: str = "data/gate/approvals.db"
    audit_path: str = "data/gate/audit.jsonl"
    missing: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return not self.missing

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GateSettings:
        env = os.environ if env is None else env
        missing = [n for n in ("FLAGSMITH_API_KEY", "APPROVAL_SIGNING_SECRET", "GATE_PROJECT_ID",
                               "GATE_ENVIRONMENTS") if not env.get(n)]
        environments: dict[str, int] = {}
        for item in filter(None, (s.strip() for s in env.get("GATE_ENVIRONMENTS", "").split(","))):
            name, _, env_id = item.rpartition(":")
            if name and env_id.strip().isdigit():
                environments[name.strip()] = int(env_id)
        server_keys = {n: env[_env_key(n)] for n in environments if env.get(_env_key(n))}
        missing += [_env_key(n) for n in environments if n not in server_keys]
        project_id = env.get("GATE_PROJECT_ID", "0")
        if env.get("GATE_PROJECT_ID") and not project_id.isdigit():
            missing.append("GATE_PROJECT_ID")  # must be the numeric id; 0 would be a wildcard
        return cls(
            scope=GateScope(project_id=int(project_id) if project_id.isdigit() else 0,
                            project_name=env.get("GATE_PROJECT_NAME", ""),
                            environments=environments),
            api_key=env.get("FLAGSMITH_API_KEY", ""),
            signing_secret=env.get("APPROVAL_SIGNING_SECRET", ""),
            server_keys=server_keys, mcp_url=env.get("FLAGSMITH_MCP_URL", DEFAULT_MCP_URL),
            db_path=env.get("GATE_DB_PATH", "data/gate/approvals.db"),
            audit_path=env.get("GATE_AUDIT_PATH", "data/gate/audit.jsonl"), missing=missing)


@dataclass
class GateDeps:
    scope: GateScope
    mcp: Any
    ids: IdResolver
    approvals: ApprovalStore
    audit: AuditLog
    fingerprint: Callable[[str], str]
    document: Callable[[str], dict[str, Any]]
    settle_reads: int = 8  # after a write, read the edge document up to this many times
    settle_interval_s: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    settle_budget_s: float = 10.0  # wall-clock cap for settle loops (register and write)

    @classmethod
    def from_settings(cls, s: GateSettings) -> GateDeps:
        mcp = McpClient(s.mcp_url, s.api_key)

        def document(environment: str) -> dict[str, Any]:
            # Short timeout: these reads run inside settle loops with a ~10 s budget.
            doc = fetch_environment_document(s.server_keys[environment], timeout=4.0)
            if doc.get("id") != s.scope.environments[environment]:
                raise FingerprintError(f"the server key for {environment} belongs to "
                                       "another environment")
            return doc

        return cls(scope=s.scope, mcp=mcp, ids=McpIdResolver(mcp, project_id=s.scope.project_id),
                   approvals=ApprovalStore(s.db_path, secret=s.signing_secret, scope=s.scope),
                   audit=AuditLog(s.audit_path), document=document,
                   fingerprint=lambda environment: fingerprint_of(document(environment)))


class ReadRequest(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class WriteRequest(BaseModel):
    approval: Approval
    ops: list[WriteOp]
    # Only has to agree with the gate's registration; it is never the reference.
    fingerprint: str | None = None


class RenderRequest(BaseModel):
    ops: list[WriteOp]
    proposal_id: str


def _environment_name(scope: GateScope, value: Any) -> str:
    for name, env_id in scope.environments.items():
        if str(value) in (name, str(env_id)):
            return name
    raise ToolDenied(f"environment {value!r} is outside the gate's scope")


def _settled(d: GateDeps, environment: str, attempts: int = 6) -> str:
    """Two equal reads in a row (the edge document lags writes by a few seconds), within the
    settle time budget so /gate/register answers well inside callers' 30 s timeouts."""
    deadline = d.monotonic() + d.settle_budget_s
    previous, lagging = None, False
    for _ in range(attempts):
        current = d.fingerprint(environment)
        # Right after a write the edge can still show the state the gate just replaced;
        # never register a proposal against that.
        lagging = d.approvals.was_written_over(environment, current)
        if current == previous and not lagging:
            return current
        previous = current
        if d.monotonic() + 1.0 >= deadline:
            break
        d.sleep(1.0)
    if lagging:
        raise FingerprintError(f"{environment} still shows the state before the last write; "
                               "try again in a few seconds")
    raise FingerprintError(f"{environment} did not settle; try again shortly")


def create_app(deps: GateDeps | None = None, settings: GateSettings | None = None,
               shared_token: str | None = None) -> FastAPI:
    if deps is None:
        settings = settings or GateSettings.from_env()
        if settings.configured:
            deps = GateDeps.from_settings(settings)
    if shared_token is None:  # from the environment: set-but-blank is a mistake, not "off"
        raw = os.environ.get("GATE_SHARED_TOKEN")
        token = (raw or "").strip()
        auth_mode = "off" if raw is None else ("token" if token else "misconfigured")
    else:
        token = shared_token.strip()
        auth_mode = "token" if token else "off"

    def authorised(authorization: str = Header(default="")) -> None:
        if auth_mode == "misconfigured":
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "GATE_SHARED_TOKEN is set but empty; refusing to run open")
        if token and not hmac.compare_digest(authorization.encode(),
                                             f"Bearer {token}".encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "gate token required")

    app = FastAPI(title="TrustGate gate", version="0.1.0")
    app.state.deps = deps

    def need() -> GateDeps:
        if app.state.deps is None:
            missing = ", ".join(settings.missing) if settings else "configuration"
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                f"gate not configured: missing {missing}")
        return app.state.deps

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "live" if app.state.deps else "unconfigured",
                "auth": auth_mode}

    @app.post("/gate/read", dependencies=[Depends(authorised)])
    def read(req: ReadRequest) -> dict[str, Any]:
        d = need()
        try:
            kind = check_read(req.tool, req.args, d.scope)
        except ToolDenied as exc:
            d.audit.append(actor="investigator", action="refused", tool=req.tool,
                           args=req.args if isinstance(req.args, dict) else {}, result=str(exc))
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
        try:
            if kind is ReadKind.MCP:
                result = d.mcp.call_tool(req.tool, req.args, is_write=False)
                body: Any = result.text() if result.is_error else _json_or_text(result)
                outcome = "error" if result.is_error else "ok"
            elif req.tool == "get_environment_document":
                env = _environment_name(d.scope, req.args.get("environment"))
                doc = d.document(env)
                body = {"environment": env, "fingerprint": fingerprint_of(doc),
                        "view": canonical_view(doc)}
                outcome = "ok"
            else:  # get_audit_log
                body = _audit_log(d, req.args)
                outcome = "ok"
        except ToolDenied as exc:
            d.audit.append(actor="investigator", action="refused", tool=req.tool, args=req.args,
                           result=str(exc))
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
        except (McpError, FingerprintError, WriterError) as exc:
            d.audit.append(actor="investigator", action="read", tool=req.tool, args=req.args,
                           result=f"error: {exc}")
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
        d.audit.append(actor="investigator", action="read", tool=req.tool, args=req.args,
                       result=outcome)
        return {"tool": req.tool, "is_error": outcome == "error", "result": body}

    @app.post("/gate/write", dependencies=[Depends(authorised)])
    def write(req: WriteRequest) -> dict[str, Any]:
        d = need()
        executor = Executor(approvals=d.approvals, scope=d.scope, ids=d.ids, mcp=d.mcp,
                            fingerprint=d.fingerprint, audit=d.audit,
                            settle_reads=d.settle_reads, settle_interval_s=d.settle_interval_s,
                            sleep=d.sleep, monotonic=d.monotonic,
                            settle_budget_s=d.settle_budget_s)
        try:
            return executor.execute(req.approval, req.ops, req.fingerprint).to_dict()
        except ApprovalRefused as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                {"reason": str(exc.reason), "detail": exc.detail}) from None
        except StaleState as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        except WriteFailed as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.result.to_dict()) from None

    @app.get("/gate/fingerprint", dependencies=[Depends(authorised)])
    def fingerprint(environment: str) -> dict[str, str]:
        d = need()
        try:
            name = _environment_name(d.scope, environment)
            return {"environment": name, "fingerprint": d.fingerprint(name)}
        except ToolDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
        except FingerprintError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None

    @app.post("/gate/render", dependencies=[Depends(authorised)])
    def render_calls(req: RenderRequest) -> dict[str, Any]:
        d = need()
        try:
            calls = render(req.ops, d.scope, d.ids, proposal_id=req.proposal_id,
                           approval_id="<pending>")
        except WriterError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        return {"calls": [c.to_dict() for c in calls]}

    @app.post("/gate/register", dependencies=[Depends(authorised)])
    def register(req: RenderRequest) -> dict[str, Any]:
        d = need()

        def refuse(code: int, detail: str) -> HTTPException:
            d.audit.append(actor="orchestrator", action="refused", tool="register_proposal",
                           args={}, result=detail[:300], proposal_id=req.proposal_id)
            return HTTPException(code, detail)

        try:
            calls = render(req.ops, d.scope, d.ids, proposal_id=req.proposal_id,
                           approval_id="<pending>")
            environment = req.ops[0].environment
            fingerprint = _settled(d, environment)
            ph = payload_hash(req.ops)
            d.approvals.register_proposal(req.proposal_id, ph, fingerprint, environment)
        except RegistrationConflict as exc:
            raise refuse(status.HTTP_409_CONFLICT, str(exc)) from None
        except FingerprintError as exc:
            raise refuse(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
        except (WriterError, McpError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise refuse(status.HTTP_422_UNPROCESSABLE_CONTENT,
                         f"{type(exc).__name__}: {exc}") from None
        d.audit.append(actor="orchestrator", action="read", tool="register_proposal",
                       args={"environment": environment}, result="registered",
                       proposal_id=req.proposal_id, payload_hash=ph, fingerprint=fingerprint)
        return {"proposal_id": req.proposal_id, "payload_hash": ph, "fingerprint": fingerprint,
                "calls": [c.to_dict() for c in calls]}

    @app.get("/gate/audit", dependencies=[Depends(authorised)])
    def audit(proposal_id: str | None = None) -> dict[str, Any]:
        # Deliberately not audited itself: the UI polls this every couple of seconds.
        return {"gate": need().audit.entries(proposal_id=proposal_id)}

    return app


def _json_or_text(result: Any) -> Any:
    try:
        return result.json()
    except ValueError:
        return result.text()


def _audit_log(d: GateDeps, args: dict[str, Any]) -> dict[str, Any]:
    entries = d.audit.entries(proposal_id=args.get("proposal_id"))
    versions: list[dict[str, Any]] = []
    if args.get("environment") and args.get("features"):
        env_id = d.scope.environments[_environment_name(d.scope, args["environment"])]
        for feature in args["features"]:
            result = d.mcp.call_tool("get_environment_feature_versions",
                                     {"environment_pk": env_id,
                                      "feature_pk": d.ids.feature_id(feature)}, is_write=False)
            if not result.is_error:
                payload = _json_or_text(result)
                rows = payload.get("results", []) if isinstance(payload, dict) else []
                versions += [dict(r, feature=feature) for r in rows]
    return merge_versions(entries, versions, known_entries=d.audit.entries())


app = create_app()
