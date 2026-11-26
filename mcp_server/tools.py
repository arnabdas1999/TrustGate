"""The TrustGate MCP server's tools. Read and propose only: there is no write tool.

- investigate_incident / get_investigation: start and follow an investigation (TrustGate API)
- the allowlisted Flagsmith reads: proxied to the gate's POST /gate/read, which enforces the
  allowlist and scope again. This server holds no Flagsmith key.
- propose_change: returns the pending proposal and a link where a human approves it. It never
  approves or writes. Approval needs the approver's password session in the UI.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# Must equal gate.allowlist.MCP_READS | GATE_NATIVE_READS (a unit test checks it). Kept as a
# literal so this server never imports gate internals.
READ_TOOLS: frozenset[str] = frozenset({
    "list_project_features", "get_feature_flag", "list_project_segments",
    "get_project_segment", "list_feature_segments", "list_project_environments",
    "get_environment_feature_versions", "get_environment_feature_version_states",
    "get_audit_log", "get_environment_document",
})

_ANY_ARGS = {"type": "object", "additionalProperties": True}

TOOLS: list[dict[str, Any]] = [
    {"name": "investigate_incident",
     "description": "Start a TrustGate investigation of a captured incident. It replays the "
                    "failing agent in a sandbox to find which flag change broke it, then "
                    "prepares a repair proposal. Nothing in Flagsmith changes.",
     "inputSchema": {"type": "object", "required": ["incident_id"], "properties": {
         "incident_id": {"type": "string"},
         "mode": {"type": "string", "enum": ["guided", "plain"], "default": "guided"}}}},
    {"name": "get_investigation",
     "description": "Status, suspects, culprit set, search budget and proposal of an "
                    "investigation.",
     "inputSchema": {"type": "object", "required": ["investigation_id"],
                     "properties": {"investigation_id": {"type": "string"}}}},
    {"name": "propose_change",
     "description": "Get the repair TrustGate proposes for an investigation and the link where "
                    "a human approves it. This never changes Flagsmith: only an approver can.",
     "inputSchema": {"type": "object", "required": ["investigation_id"],
                     "properties": {"investigation_id": {"type": "string"}}}},
    *[{"name": name,
       "description": f"Allowlisted Flagsmith read ({name}), served by the TrustGate gate, "
                      "which checks the project and environment scope.",
       "inputSchema": _ANY_ARGS} for name in sorted(READ_TOOLS)],
]


class ToolError(Exception):
    pass


class Backend:
    """HTTP access to the TrustGate API (investigations) and the gate (reads). No keys."""

    def __init__(self, *, api_url: str, gate_url: str, web_url: str,
                 client: httpx.Client | None = None):
        self.api_url, self.gate_url = api_url.rstrip("/"), gate_url.rstrip("/")
        self.web_url = web_url.rstrip("/")
        self._http = client or httpx.Client(timeout=30)

    def _json(self, response: httpx.Response, what: str) -> Any:
        if response.status_code >= 400:
            raise ToolError(f"{what} returned HTTP {response.status_code}: "
                            f"{response.text[:300]}")
        return response.json()

    def _do(self, method: str, url: str, what: str, **kwargs: Any) -> Any:
        try:
            return self._json(self._http.request(method, url, **kwargs), what)
        except httpx.HTTPError as exc:
            raise ToolError(f"{what}: {type(exc).__name__}") from None

    def start_investigation(self, incident_id: str, mode: str) -> Any:
        return self._do("POST", f"{self.api_url}/api/investigations", "start investigation",
                        json={"incident_id": incident_id, "mode": mode})

    def investigation(self, investigation_id: str) -> Any:
        return self._do("GET", f"{self.api_url}/api/investigations/{investigation_id}",
                        f"investigation {investigation_id}")

    def gate_read(self, tool: str, args: dict[str, Any]) -> Any:
        return self._do("POST", f"{self.gate_url}/gate/read", f"gate read {tool}",
                        json={"tool": tool, "args": args})


def _summary(view: dict[str, Any]) -> dict[str, Any]:
    inv, proposal = view["investigation"], view.get("proposal")
    keys = ("id", "status", "segment", "suspects", "culprit_set", "runs_used", "usd_used")
    out = {k: inv.get(k) for k in keys}
    if proposal:
        out["proposal"] = {k: proposal.get(k) for k in ("id", "state", "explanation")}
    return out


def call_tool(backend: Backend, name: str, args: dict[str, Any]) -> Any:
    if name == "investigate_incident":
        return backend.start_investigation(str(args["incident_id"]),
                                           str(args.get("mode", "guided")))
    if name == "get_investigation":
        return _summary(backend.investigation(str(args["investigation_id"])))
    if name == "propose_change":
        inv_id = str(args["investigation_id"])
        view = backend.investigation(inv_id)
        proposal = view.get("proposal")
        if not proposal or proposal.get("state") != "proposed":
            raise ToolError(f"investigation is {view['investigation']['status']}; "
                            "no proposal is waiting for approval")
        return {"investigation_id": inv_id, "proposal_id": proposal["id"],
                "explanation": proposal.get("explanation"),
                "proposed_ops": proposal["candidate"]["ops"],
                "expires_at": proposal.get("expires_at"),
                "needs_human_approval": True,
                "approval_link": f"{backend.web_url}/investigations/{inv_id}",
                "note": "TrustGate's MCP server cannot write. A human approves this exact "
                        "change on the page; the gate then runs it once."}
    if name in READ_TOOLS:
        return backend.gate_read(name, args)
    raise ToolError(f"unknown tool {name!r}")


def result_content(payload: Any, *, is_error: bool) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}
