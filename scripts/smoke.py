"""End-to-end smoke test of one investigation through the TrustGate API.

Used at every checkpoint from CP5:

    uv run python -m scripts.smoke [--api http://localhost:8000] [--investigation ID]
                                   [--incident PATH] [--timeout 300] [--poll 1.0]

The approver password comes from the APPROVER_PASSWORD environment variable only. It is never
accepted on the command line and never printed.

Exit codes: 0 passed, 1 failed, 2 APPROVER_PASSWORD missing (or a bad --incident file).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from api.schemas import Health, InvestigationView
from contracts import Incident, Investigation, InvestigationStatus, SessionResponse

# Statuses that end the wait for a proposal without one.
PROPOSAL_FAILURES = frozenset({
    InvestigationStatus.FAILED, InvestigationStatus.INCONCLUSIVE,
    InvestigationStatus.NOT_REPRODUCIBLE, InvestigationStatus.REJECTED,
    InvestigationStatus.EXPIRED, InvestigationStatus.STALE,
})
# Statuses that end the wait for verification without it.
VERIFY_FAILURES = frozenset({
    InvestigationStatus.FAILED, InvestigationStatus.STALE, InvestigationStatus.EXPIRED,
    InvestigationStatus.REJECTED,
})


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    elapsed_ms: int


@dataclass
class SmokeResult:
    passed: bool
    steps: list[Step] = field(default_factory=list)


class _StepFailed(Exception):
    pass


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:200]
    return str(body)[:200]


def _expect(response: httpx.Response, what: str, ok: tuple[int, ...] = (200,)) -> Any:
    if response.status_code not in ok:
        raise _StepFailed(f"{what}: HTTP {response.status_code}: {_detail(response)}")
    try:
        return response.json()
    except ValueError as exc:
        raise _StepFailed(f"{what}: response is not JSON") from exc


def run_smoke(
    client: httpx.Client,
    password: str,
    investigation_id: str | None = None,
    incident: dict[str, Any] | None = None,
    timeout_s: float = 300,
    poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SmokeResult:
    """Drive one investigation from proposal to verified read-back.

    `client` must have the API as its base URL (a FastAPI TestClient works too).
    """
    result = SmokeResult(passed=False)
    state: dict[str, Any] = {}

    def redact(text: str) -> str:
        return text.replace(password, "***") if password else text

    def step(name: str, fn: Callable[[], str]) -> bool:
        start = clock()
        try:
            detail, ok = fn(), True
        except _StepFailed as exc:
            detail, ok = str(exc), False
        except httpx.HTTPError as exc:
            detail, ok = f"request failed: {type(exc).__name__}: {exc}", False
        except ValidationError as exc:
            detail, ok = f"unexpected response shape: {exc.error_count()} validation errors", False
        elapsed_ms = int((clock() - start) * 1000)
        result.steps.append(Step(name, ok, redact(detail), elapsed_ms))
        return ok

    def get_view(inv_id: str) -> InvestigationView:
        body = _expect(client.get(f"/api/investigations/{inv_id}"), f"GET investigation {inv_id}")
        return InvestigationView.model_validate(body)

    def poll(inv_id: str, target: InvestigationStatus,
             failures: frozenset[InvestigationStatus]) -> InvestigationView:
        deadline = clock() + timeout_s
        while True:
            view = get_view(inv_id)
            status = view.investigation.status
            if status == target:
                return view
            if status in failures:
                raise _StepFailed(f"{inv_id} ended as {status.value}, not {target.value}")
            if clock() >= deadline:
                raise _StepFailed(
                    f"timed out after {timeout_s:g}s waiting for {target.value}; "
                    f"{inv_id} is still {status.value}")
            sleep(poll_s)

    # 1. health ------------------------------------------------------------------------------
    def health() -> str:
        body = Health.model_validate(_expect(client.get("/api/health"), "GET /api/health"))
        if body.status != "ok":
            raise _StepFailed(f"health status is {body.status!r}")
        state["mock"] = body.mock
        return f"ok, mock={str(body.mock).lower()}"

    # 2. find or create a proposed investigation ---------------------------------------------
    def create_incident() -> str:
        body = _expect(client.post("/api/incidents", json=incident), "POST /api/incidents",
                       ok=(200, 201))
        state["incident_id"] = body["id"]
        return f"incident {body['id']}"

    def start_investigation() -> str:
        body = _expect(
            client.post("/api/investigations",
                        json={"incident_id": state["incident_id"], "mode": "guided"}),
            "POST /api/investigations", ok=(200, 201))
        investigation = Investigation.model_validate(body)
        state["investigation_id"] = investigation.id
        return f"investigation {investigation.id} ({investigation.status.value})"

    def wait_for_proposal() -> str:
        view = poll(state["investigation_id"], InvestigationStatus.PROPOSED, PROPOSAL_FAILURES)
        return _remember_proposal(view)

    def find_investigation() -> str:
        if investigation_id:
            view = get_view(investigation_id)
            status = view.investigation.status
            if status != InvestigationStatus.PROPOSED:
                raise _StepFailed(f"{investigation_id} is {status.value}, not proposed")
            state["investigation_id"] = investigation_id
            return _remember_proposal(view)
        body = _expect(client.get("/api/investigations"), "GET /api/investigations")
        listed = [Investigation.model_validate(item) for item in body]
        proposed = [i for i in listed if i.status == InvestigationStatus.PROPOSED]
        if not proposed:
            raise _StepFailed(
                f"no investigation in status proposed ({len(listed)} listed); "
                "pass --investigation or --incident")
        state["investigation_id"] = proposed[0].id
        return _remember_proposal(get_view(proposed[0].id))

    def _remember_proposal(view: InvestigationView) -> str:
        if view.proposal is None:
            raise _StepFailed(f"{view.investigation.id} is proposed but has no proposal")
        state["proposal_id"] = view.proposal.id
        return f"{view.investigation.id} -> proposal {view.proposal.id}"

    # 3. approver session --------------------------------------------------------------------
    def login() -> str:
        response = client.post("/api/session", json={"password": password})
        if response.status_code == 401:
            raise _StepFailed("login refused (401): wrong APPROVER_PASSWORD")
        if response.status_code == 503:
            raise _StepFailed("login disabled (503): APPROVER_PASSWORD is not set on the server")
        session = SessionResponse.model_validate(_expect(response, "POST /api/session"))
        state["token"] = session.token
        return f"logged in as {session.approver}"

    # 4. approve -----------------------------------------------------------------------------
    def approve() -> str:
        proposal_id = state["proposal_id"]
        response = client.post(f"/api/proposals/{proposal_id}/approve",
                               headers={"Authorization": f"Bearer {state['token']}"})
        if response.status_code == 409:
            raise _StepFailed(f"approve refused (409): {_detail(response)}")
        body = _expect(response, f"POST approve {proposal_id}")
        return f"{proposal_id} is {body.get('state')}"

    # 5. wait for verification ---------------------------------------------------------------
    def wait_for_verified() -> str:
        view = poll(state["investigation_id"], InvestigationStatus.VERIFIED, VERIFY_FAILURES)
        state["view"] = view
        return f"{view.investigation.id} verified"

    # 6. read-back ---------------------------------------------------------------------------
    def readback() -> str:
        view: InvestigationView = state["view"]
        if view.proposal is None:
            raise _StepFailed("verified investigation has no proposal")
        entries = view.proposal.readback
        if not entries:
            raise _StepFailed("proposal read-back is empty")
        defaulted = [r.identity.identifier for r in entries if r.from_default_handler]
        if defaulted:
            raise _StepFailed(
                f"read-back came from the default handler for: {', '.join(defaulted)}")
        return f"{len(entries)} identities read back from Flagsmith"

    plan: list[tuple[str, Callable[[], str]]] = [("health", health)]
    if incident is not None:
        plan += [("create incident", create_incident),
                 ("start investigation", start_investigation),
                 ("wait for proposal", wait_for_proposal)]
    else:
        plan.append(("find investigation", find_investigation))
    plan += [("session", login), ("approve", approve),
             ("wait for verified", wait_for_verified), ("readback", readback)]

    for name, fn in plan:
        if not step(name, fn):
            return result
    result.passed = True
    return result


def format_result(result: SmokeResult) -> str:
    width = max([len(s.name) for s in result.steps] + [4])
    lines = [f"{'step'.ljust(width)}  {'ok':4}  {'ms':>7}  detail"]
    for s in result.steps:
        mark = "ok" if s.ok else "FAIL"
        lines.append(f"{s.name.ljust(width)}  {mark:4}  {s.elapsed_ms:>7}  {s.detail}")
    lines.append("SMOKE PASSED" if result.passed else "SMOKE FAILED")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.smoke",
        description="End-to-end smoke test of one investigation through the TrustGate API. "
                    "Reads the approver password from APPROVER_PASSWORD.")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--investigation", help="investigation id to approve (status proposed)")
    parser.add_argument("--incident", type=Path,
                        help="incident JSON (contracts.Incident) to start a new investigation")
    parser.add_argument("--timeout", type=float, default=300, help="seconds per wait (300)")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between polls (1.0)")
    args = parser.parse_args(argv)

    password = os.environ.get("APPROVER_PASSWORD", "")
    if not password:
        print("APPROVER_PASSWORD is not set; export it before running the smoke test.",
              file=sys.stderr)
        return 2

    incident: dict[str, Any] | None = None
    if args.incident is not None:
        try:
            raw = json.loads(args.incident.read_text(encoding="utf-8"))
            incident = Incident.model_validate(raw).model_dump(mode="json")
        except (OSError, ValueError) as exc:
            print(f"cannot use --incident {args.incident}: {exc}", file=sys.stderr)
            return 2

    with httpx.Client(base_url=args.api, timeout=30) as client:
        result = run_smoke(client, password, investigation_id=args.investigation,
                           incident=incident, timeout_s=args.timeout, poll_s=args.poll)
    print(format_result(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
