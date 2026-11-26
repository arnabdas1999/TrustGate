"""Where the orchestrator keeps incidents, investigations and proposals.

SqliteRepo is the persistent store. DictRepo writes into the API's in-memory Store, so the
API and the orchestrator see the same objects until api/store.py moves to SQLite. Both keep
the exact MCP calls the gate rendered for each proposal: Proposal has no field for them yet.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from contracts.models import Incident, Investigation, Proposal

M = TypeVar("M", bound=BaseModel)


class Repo(Protocol):
    def get_incident(self, incident_id: str) -> Incident | None: ...
    def put_incident(self, incident: Incident) -> None: ...
    def get_investigation(self, investigation_id: str) -> Investigation | None: ...
    def put_investigation(self, investigation: Investigation) -> None: ...
    def get_proposal(self, proposal_id: str) -> Proposal | None: ...
    def put_proposal(self, proposal: Proposal) -> None: ...
    def get_calls(self, proposal_id: str) -> list[dict[str, Any]]: ...
    def put_calls(self, proposal_id: str, calls: list[dict[str, Any]]) -> None: ...


_TABLES = {"incidents": Incident, "investigations": Investigation, "proposals": Proposal}


class SqliteRepo:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._conn:
            for table in [*_TABLES, "proposal_calls"]:
                self._conn.execute(f"CREATE TABLE IF NOT EXISTS {table} "  # noqa: S608
                                   "(id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def _get(self, table: str, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(f"SELECT body FROM {table} WHERE id = ?",  # noqa: S608
                                     (key,)).fetchone()
        return row[0] if row else None

    def _put(self, table: str, key: str, body: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)",  # noqa: S608
                               (key, body))

    def _model(self, table: str, key: str) -> Any:
        body = self._get(table, key)
        return _TABLES[table].model_validate_json(body) if body else None

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._model("incidents", incident_id)

    def put_incident(self, incident: Incident) -> None:
        self._put("incidents", incident.id, incident.model_dump_json())

    def get_investigation(self, investigation_id: str) -> Investigation | None:
        return self._model("investigations", investigation_id)

    def put_investigation(self, investigation: Investigation) -> None:
        self._put("investigations", investigation.id, investigation.model_dump_json())

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._model("proposals", proposal_id)

    def put_proposal(self, proposal: Proposal) -> None:
        self._put("proposals", proposal.id, proposal.model_dump_json())

    def get_calls(self, proposal_id: str) -> list[dict[str, Any]]:
        body = self._get("proposal_calls", proposal_id)
        return json.loads(body) if body else []

    def put_calls(self, proposal_id: str, calls: list[dict[str, Any]]) -> None:
        self._put("proposal_calls", proposal_id, json.dumps(calls))


class DictRepo:
    """Adapter over api.store.Store (incidents / investigations / proposals dicts)."""

    def __init__(self, store: Any):
        self._store = store
        self._calls: dict[str, list[dict[str, Any]]] = {}

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._store.incidents.get(incident_id)

    def put_incident(self, incident: Incident) -> None:
        self._store.incidents[incident.id] = incident

    def get_investigation(self, investigation_id: str) -> Investigation | None:
        return self._store.investigations.get(investigation_id)

    def put_investigation(self, investigation: Investigation) -> None:
        self._store.investigations[investigation.id] = investigation

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._store.proposals.get(proposal_id)

    def put_proposal(self, proposal: Proposal) -> None:
        self._store.proposals[proposal.id] = proposal

    def get_calls(self, proposal_id: str) -> list[dict[str, Any]]:
        return self._calls.get(proposal_id, [])

    def put_calls(self, proposal_id: str, calls: list[dict[str, Any]]) -> None:
        self._calls[proposal_id] = calls
