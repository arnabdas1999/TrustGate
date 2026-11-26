"""HTTP client for the gate service, used by the API and the orchestrator.

Nothing outside gate/ imports gate internals or holds the Flagsmith organisation key: this
client only calls the gate's internal endpoints (/gate/read, /gate/fingerprint, /gate/render).
Writes stay in api.main._send_to_gate, which signs the approval first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import ValidationError

from contracts.models import AuditEntry, WriteOp


class GateError(Exception):
    pass


class GateClient:
    def __init__(self, base_url: str, *, client: httpx.Client | None = None,
                 timeout: float = 30.0):
        self._http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GateError(f"gate unreachable: {type(exc).__name__}") from None
        if r.status_code >= 400:
            raise GateError(f"gate {path} returned HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def fingerprint(self, environment: str) -> str:
        return self._call("GET", "/gate/fingerprint",
                          params={"environment": environment})["fingerprint"]

    def render(self, ops: Sequence[WriteOp], proposal_id: str) -> list[dict[str, Any]]:
        body = {"ops": [op.model_dump(mode="json") for op in ops], "proposal_id": proposal_id}
        return self._call("POST", "/gate/render", json=body)["calls"]

    def read(self, tool: str, args: dict[str, Any]) -> Any:
        return self._call("POST", "/gate/read", json={"tool": tool, "args": args})["result"]

    def audit_entries(self, environment: str, features: Sequence[str]) -> list[AuditEntry]:
        """The gate's audit trail as API AuditEntry rows (lines that don't fit are skipped)."""
        result = self.read("get_audit_log", {"environment": environment,
                                             "features": list(features)})
        entries = []
        for line in result.get("gate", []):
            try:
                entries.append(AuditEntry.model_validate(line))
            except ValidationError:
                continue
        return entries
