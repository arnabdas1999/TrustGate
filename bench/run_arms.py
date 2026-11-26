"""Run the automated benchmark arms and append one RunRecord per (case, arm).

    uv run python -m bench.run_arms --cases opspilot-eval-1,opspilot-eval-2 --arms plain,guided

plain and guided go through the API like a real alert: capture the incident, start an
investigation in that mode, wait for a proposal, then wait for a human to approve or reject it
in the UI. This script never approves anything itself.

global_rollback is computed in the sandbox: revert the whole release and score the held-out
cases. It needs sandbox.evaluate_global_rollback (Khyati) and is skipped until that exists.

Reads bench/incidents/<case>.json (a captured contracts.Incident, written by the sandbox) and,
for global_rollback, bench/releases/<case>.json. Appends to bench/records/runs.jsonl.
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from api.schemas import InvestigationView
from bench.records import RUNS_FILE, Arm, RunRecord, append
from contracts import Incident, InvestigationMode, InvestigationStatus, TestPhase

BENCH_DIR = Path(__file__).resolve().parent
INCIDENTS_DIR = BENCH_DIR / "incidents"
RELEASES_DIR = BENCH_DIR / "releases"

AUTOMATED_ARMS = (Arm.PLAIN, Arm.GUIDED, Arm.GLOBAL_ROLLBACK)
API_ARMS = {Arm.PLAIN: InvestigationMode.PLAIN, Arm.GUIDED: InvestigationMode.GUIDED}

_S = InvestigationStatus
TERMINAL = frozenset({_S.FAILED, _S.INCONCLUSIVE, _S.NOT_REPRODUCIBLE, _S.REJECTED, _S.EXPIRED,
                      _S.STALE})
DONE = TERMINAL | {_S.VERIFIED}
# A proposal exists (a fast human may already have approved it between two polls).
PROPOSAL_REACHED = DONE | {_S.PROPOSED, _S.APPROVED, _S.APPLIED}
DIAGNOSTIC_PHASES = frozenset({TestPhase.SEARCH, TestPhase.ESCALATE})

Sleep = Callable[[float], None]
Clock = Callable[[], float]  # monotonic seconds
Now = Callable[[], datetime]
Emit = Callable[[str], None]


class WaitForDecision(Protocol):
    """Waits while a human approves or rejects the proposal in the UI. Returns the last view."""

    def __call__(self, client: httpx.Client, investigation_id: str, *, timeout: float,
                 poll: float, sleep: Sleep, clock: Clock) -> InvestigationView: ...


class GlobalRollbackEvaluator(Protocol):
    """TODO(khyati): provide this as sandbox.evaluate_global_rollback.

    Replays the incident with every flag in the release reverted to its old value, plus the
    held-out cases, and returns (heldout_passes, heldout_runs, cost_kept). cost_kept is the
    share of the release's cost saving kept, 0 to 1 (0.0 for a full rollback).
    """

    def __call__(self, incident: Incident, release: dict[str, Any]) -> tuple[int, int, float]: ...


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_incident(case: str, incidents_dir: Path = INCIDENTS_DIR) -> Incident:
    """Raises FileNotFoundError if the sandbox has not captured this case yet."""
    path = incidents_dir / f"{case}.json"
    if not path.exists():
        raise FileNotFoundError(f"no captured incident at {path} (the sandbox writes it)")
    return Incident.model_validate_json(path.read_text(encoding="utf-8"))


def load_release(case: str, releases_dir: Path = RELEASES_DIR) -> dict[str, Any]:
    path = releases_dir / f"{case}.json"
    if not path.exists():
        raise FileNotFoundError(f"no release at {path} (bench.make_incident writes it)")
    return json.loads(path.read_text(encoding="utf-8"))


def load_global_rollback_evaluator() -> GlobalRollbackEvaluator | None:
    """sandbox.evaluate_global_rollback, or None while the sandbox does not provide it."""
    try:
        module = importlib.import_module("sandbox")
    except ImportError:
        return None
    return getattr(module, "evaluate_global_rollback", None)


def parse_arms(text: str) -> list[Arm]:
    arms: list[Arm] = []
    for name in (n.strip() for n in text.split(",")):
        if not name:
            continue
        try:
            arm = Arm(name)
        except ValueError:
            choices = ", ".join(a.value for a in AUTOMATED_ARMS)
            raise ValueError(f"unknown arm {name!r}; choose from {choices}") from None
        if arm not in AUTOMATED_ARMS:
            raise ValueError("manual runs are typed into bench/manual_log.csv, not run here")
        if arm not in arms:
            arms.append(arm)
    return arms


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def fetch_view(client: httpx.Client, investigation_id: str) -> InvestigationView:
    response = client.get(f"/api/investigations/{investigation_id}")
    response.raise_for_status()
    return InvestigationView.model_validate(response.json())


def fetch_active_ms(client: httpx.Client, investigation_id: str) -> int:
    response = client.get(f"/api/activity/{investigation_id}")
    response.raise_for_status()
    return int(response.json()["active_ms"])


def approver_from_audit(client: httpx.Client, proposal_id: str) -> str | None:
    """Who approved or rejected a proposal, read from the audit trail."""
    response = client.get("/api/audit")
    response.raise_for_status()
    for entry in response.json():
        if entry.get("proposal_id") == proposal_id and entry.get("action") in ("approve",
                                                                               "reject"):
            return entry.get("actor")
    return None


def poll_until(client: httpx.Client, investigation_id: str, stop: Iterable[InvestigationStatus],
               *, timeout: float, poll: float, sleep: Sleep, clock: Clock) -> InvestigationView:
    """Poll until the status is in `stop` or `timeout` seconds pass. Returns the last view."""
    stop = frozenset(stop)
    deadline = clock() + timeout
    while True:
        view = fetch_view(client, investigation_id)
        if view.investigation.status in stop or clock() >= deadline:
            return view
        sleep(poll)


def wait_for_decision(client: httpx.Client, investigation_id: str, *, timeout: float,
                      poll: float, sleep: Sleep, clock: Clock) -> InvestigationView:
    """Default WaitForDecision: poll until verified or terminal. Never approves anything."""
    return poll_until(client, investigation_id, DONE, timeout=timeout, poll=poll, sleep=sleep,
                      clock=clock)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def record_from_view(case: str, arm: Arm | str, view: InvestigationView, active_ms: int | None,
                     approver: str | None) -> RunRecord:
    """Pure: turn the final investigation view into a RunRecord."""
    inv = view.investigation
    ts = inv.timestamps
    verified = inv.status == InvestigationStatus.VERIFIED
    candidate = view.proposal.candidate if view.proposal is not None else None
    if inv.status in DONE:
        notes = f"final status: {inv.status.value}"
    else:
        notes = f"timed out in status {inv.status.value}"
    fresh_failed: bool | None = None
    if verified:
        fresh_failed = False
    elif inv.status == InvestigationStatus.FAILED and ts.approved is not None:
        fresh_failed = True  # approved and applied, then the verifier's fresh runs failed
    return RunRecord(
        case=case,
        domain=inv.domain.value,
        arm=Arm(arm),
        person=approver,
        started_at=ts.alert,
        first_suspect_at=ts.first_suspect,
        applied_at=ts.verified if verified else None,
        unsolved=not verified,
        human_active_ms=active_ms,
        proposal_to_approve_ms=_ms(ts.proposal, ts.approved),
        diagnostic_runs=sum(s.runs for s in inv.steps if s.phase in DIAGNOSTIC_PHASES),
        reported_culprit=[c.flag for c in inv.culprit_set],
        validated_repair=verified,
        fresh_validation_failed=fresh_failed,
        flags_reverted=list(dict.fromkeys(op.feature for op in candidate.ops)) if candidate else [],
        cost_kept=candidate.cost_kept if candidate else None,
        inference_usd=inv.usd_used,
        investigation_id=inv.id,
        notes=notes,
    )


def summarize(case: str, arm: Arm, view: InvestigationView) -> str:
    """What the operator sees before deciding in the UI."""
    inv, proposal = view.investigation, view.proposal
    lines = [f"[{case} / {arm.value}] investigation {inv.id} is {inv.status.value}"]
    if proposal is not None:
        cand = proposal.candidate
        lines.append(f"  proposal {proposal.id} ({cand.kind.value}):")
        for op in cand.ops:
            where = f" for segment {op.segment}" if op.segment else " (environment default)"
            lines.append(f"    {op.tool} {op.feature}={op.value!r}{where} in {op.environment}")
        lines.append(f"  culprit: {', '.join(c.flag for c in inv.culprit_set) or '(none)'}")
        if cand.validation is not None:
            v = cand.validation
            lines.append(f"  validation: {v.outcome.value}, {v.failures}/{v.runs} runs failed")
        if cand.heldout_runs:
            lines.append(f"  held-out: {cand.heldout_passes}/{cand.heldout_runs} passed "
                         f"(baseline {cand.baseline_heldout_passes})")
        if cand.cost_kept is not None:
            lines.append(f"  cost saving kept: {cand.cost_kept:.0%}")
        lines.append(f"  spent: {inv.runs_used} runs, ${inv.usd_used:.2f}")
    lines.append(f"  -> Approve or reject investigation {inv.id} in the TrustGate UI. "
                 "Waiting for your decision (this script never approves).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def run_api_arm(client: httpx.Client, case: str, arm: Arm, incident: Incident, *,
                approver: str | None, timeout: float, poll: float, sleep: Sleep, clock: Clock,
                wait: WaitForDecision = wait_for_decision, emit: Emit = print) -> RunRecord:
    """One plain or guided run through the API. The approval step is a human's."""
    mode = API_ARMS[arm]
    arm_incident = incident.model_copy(update={"id": f"{incident.id}-{arm.value}"})
    client.post("/api/incidents", json=arm_incident.model_dump(mode="json")).raise_for_status()
    started = client.post("/api/investigations",
                          json={"incident_id": arm_incident.id, "mode": mode.value})
    started.raise_for_status()
    investigation_id = started.json()["id"]
    emit(f"[{case} / {arm.value}] started investigation {investigation_id}")

    view = poll_until(client, investigation_id, PROPOSAL_REACHED, timeout=timeout, poll=poll,
                      sleep=sleep, clock=clock)
    status = view.investigation.status
    if status in PROPOSAL_REACHED and status not in DONE:
        emit(summarize(case, arm, view))
        view = wait(client, investigation_id, timeout=timeout, poll=poll, sleep=sleep,
                    clock=clock)

    person = approver
    if person is None and view.proposal is not None:
        person = approver_from_audit(client, view.proposal.id)
    return record_from_view(case, arm, view, fetch_active_ms(client, investigation_id), person)


def run_global_rollback(case: str, incident: Incident, release: dict[str, Any],
                        evaluator: GlobalRollbackEvaluator, *, now: Now) -> RunRecord:
    """Revert the whole release (sandbox only, no Flagsmith write, no human step)."""
    started = now()
    passes, runs, cost_kept = evaluator(incident, release)
    flags = list(dict.fromkeys(c["flag"] for c in release.get("changes", [])))
    return RunRecord(
        case=case,
        domain=incident.domain.value,
        arm=Arm.GLOBAL_ROLLBACK,
        started_at=started,
        unsolved=False,
        diagnostic_runs=0,  # no search: it reverts everything
        flags_reverted=flags,
        cost_kept=cost_kept,
        inference_usd=0.0,
        notes=f"held-out {passes}/{runs} passed (sandbox)",
    )


def run_cases(cases: Iterable[str], arms: Iterable[Arm], *, client: httpx.Client | None,
              incidents_dir: Path = INCIDENTS_DIR, releases_dir: Path = RELEASES_DIR,
              out: Path = RUNS_FILE, approver: str | None = None, timeout: float = 1800,
              poll: float = 2, sleep: Sleep = time.sleep, clock: Clock = time.monotonic,
              now: Now = lambda: datetime.now(UTC), wait: WaitForDecision = wait_for_decision,
              load_evaluator: Callable[[], GlobalRollbackEvaluator | None] = (
                  load_global_rollback_evaluator),
              emit: Emit = print) -> list[RunRecord]:
    """Run every (case, arm), append each RunRecord to `out`, and return them."""
    arms = list(arms)
    records: list[RunRecord] = []
    evaluator: GlobalRollbackEvaluator | None = None
    if Arm.GLOBAL_ROLLBACK in arms:
        evaluator = load_evaluator()
        if evaluator is None:
            emit("skip global_rollback: needs the sandbox (Khyati); "
                 "sandbox.evaluate_global_rollback is not importable yet")

    for case in cases:
        try:
            incident = load_incident(case, incidents_dir)
        except (FileNotFoundError, ValueError) as exc:
            emit(f"skip {case}: {exc}")
            continue
        for arm in arms:
            try:
                if arm == Arm.GLOBAL_ROLLBACK:
                    if evaluator is None:
                        continue
                    release = load_release(case, releases_dir)
                    record = run_global_rollback(case, incident, release, evaluator, now=now)
                else:
                    if client is None:
                        raise RuntimeError("the plain and guided arms need an API client")
                    record = run_api_arm(client, case, arm, incident, approver=approver,
                                         timeout=timeout, poll=poll, sleep=sleep, clock=clock,
                                         wait=wait, emit=emit)
            except (FileNotFoundError, ValueError, httpx.HTTPError) as exc:
                emit(f"skip {case} / {arm.value}: {exc}")
                continue
            append(out, record)
            records.append(record)
            outcome = "unsolved" if record.unsolved else "solved"
            emit(f"logged {case} / {arm.value}: {outcome} ({record.notes}) -> {out}")
    return records


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", required=True, help="comma-separated, e.g. opspilot-eval-1")
    parser.add_argument("--arms", default="plain,guided",
                        help="comma-separated: plain, guided, global_rollback")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--approver", default=None,
                        help="name for RunRecord.person (default: read from the audit trail)")
    parser.add_argument("--timeout", type=float, default=1800,
                        help="seconds to wait for a proposal, and again for the decision")
    parser.add_argument("--poll", type=float, default=2, help="seconds between polls")
    parser.add_argument("--out", type=Path, default=RUNS_FILE)
    args = parser.parse_args(argv)

    cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    try:
        arms = parse_arms(args.arms)
    except ValueError as exc:
        parser.error(str(exc))
    if not cases or not arms:
        parser.error("give at least one case and one arm")

    with httpx.Client(base_url=args.api, timeout=30) as client:
        records = run_cases(cases, arms, client=client, out=args.out, approver=args.approver,
                            timeout=args.timeout, poll=args.poll)
    print(f"done: {len(records)} record(s) appended to {args.out}")


if __name__ == "__main__":
    main()
