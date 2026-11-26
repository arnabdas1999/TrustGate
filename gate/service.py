"""TrustGate gate service (port 8100, internal to the Docker network only).

The only process that holds the Flagsmith organisation key. Endpoints:
- POST /gate/read         allowlisted reads: {tool, args}. Default deny, audited.
- POST /gate/write        {approval, ops, fingerprint}, exactly as api.main._send_to_gate posts.
                          200 applied, 403 refused (reason), 409 stale, 502 write failed.
- GET  /gate/fingerprint  ?environment=NAME: canonical fingerprint of the edge document.
- POST /gate/render       {ops, proposal_id}: the exact MCP call(s) a write would send.
- GET  /health

Configuration comes only from the gate's environment (compose passes .env.gate):
FLAGSMITH_API_KEY, APPROVAL_SIGNING_SECRET, GATE_PROJECT_ID, GATE_PROJECT_NAME,
GATE_ENVIRONMENTS ("Development:121197,Demo Production:121198"),
GATE_SERVER_KEY_<ENV> (a ser. key per environment, e.g. GATE_SERVER_KEY_DEMO_PRODUCTION),
optional FLAGSMITH_MCP_URL, GATE_DB_PATH, GATE_AUDIT_PATH. Without them the service still
starts (so `docker compose up` works) and answers 503.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from contracts.models import Approval, WriteOp
from gate.allowlist import ReadKind, ToolDenied, check_read
from gate.approvals import ApprovalRefused, ApprovalStore
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
    settle_reads: int = 15  # after a write, read the edge document up to this many times
    settle_interval_s: float = 2.0

    @classmethod
    def from_settings(cls, s: GateSettings) -> GateDeps:
        mcp = McpClient(s.mcp_url, s.api_key)

        def document(environment: str) -> dict[str, Any]:
            doc = fetch_environment_document(s.server_keys[environment])
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
    fingerprint: str


class RenderRequest(BaseModel):
    ops: list[WriteOp]
    proposal_id: str


def _environment_name(scope: GateScope, value: Any) -> str:
    for name, env_id in scope.environments.items():
        if str(value) in (name, str(env_id)):
            return name
    raise ToolDenied(f"environment {value!r} is outside the gate's scope")


def create_app(deps: GateDeps | None = None, settings: GateSettings | None = None) -> FastAPI:
    app = FastAPI(title="TrustGate gate", version="0.1.0")
    if deps is None:
        settings = settings or GateSettings.from_env()
        if settings.configured:
            deps = GateDeps.from_settings(settings)
    app.state.deps = deps

    def need() -> GateDeps:
        if app.state.deps is None:
            missing = ", ".join(settings.missing) if settings else "configuration"
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                f"gate not configured: missing {missing}")
        return app.state.deps

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "live" if app.state.deps else "unconfigured"}

    @app.post("/gate/read")
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

    @app.post("/gate/write")
    def write(req: WriteRequest) -> dict[str, Any]:
        d = need()
        executor = Executor(approvals=d.approvals, scope=d.scope, ids=d.ids, mcp=d.mcp,
                            fingerprint=d.fingerprint, audit=d.audit,
                            settle_reads=d.settle_reads, settle_interval_s=d.settle_interval_s)
        try:
            return executor.execute(req.approval, req.ops, req.fingerprint).to_dict()
        except ApprovalRefused as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                {"reason": str(exc.reason), "detail": exc.detail}) from None
        except StaleState as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        except WriteFailed as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.result.to_dict()) from None

    @app.get("/gate/fingerprint")
    def fingerprint(environment: str) -> dict[str, str]:
        d = need()
        try:
            name = _environment_name(d.scope, environment)
            return {"environment": name, "fingerprint": d.fingerprint(name)}
        except ToolDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
        except FingerprintError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None

    @app.post("/gate/render")
    def render_calls(req: RenderRequest) -> dict[str, Any]:
        d = need()
        try:
            calls = render(req.ops, d.scope, d.ids, proposal_id=req.proposal_id,
                           approval_id="<pending>")
        except WriterError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        return {"calls": [c.to_dict() for c in calls]}

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
    return merge_versions(entries, versions)


app = create_app()
