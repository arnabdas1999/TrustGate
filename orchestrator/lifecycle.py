"""Investigation lifecycle: capture -> suspects -> search -> candidates -> proposal, then the
verifier after the approved write.

Status flow: CAPTURING -> SEARCHING -> VALIDATING -> PROPOSED -> (API: APPROVED/APPLIED)
-> VERIFIED, or FAILED with a code-built rollback proposal that needs its own approval.
Exits: NOT_REPRODUCIBLE, INCONCLUSIVE, STALE (-> reinvestigate). Timestamps go into
Investigation.timestamps: alert (API), first_suspect, proposal, approved (API), verified.

Nothing here touches Flagsmith directly: reads and fingerprints go through the gate, and
writes only happen when the API sends a signed approval to the gate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent.explain import explain
from agent.llm import LLM
from agent.ordering import order_suspects
from agent.triage import TriageError, triage
from contracts.models import (
    Domain,
    Identity,
    Investigation,
    InvestigationStatus,
    Proposal,
    ProposalState,
    RepairCandidate,
    RepairKind,
)
from orchestrator.proposals import (
    ProposalError,
    build_ops,
    build_proposal,
    expectations,
    inverse_ops,
    passing_candidates,
)
from orchestrator.repo import Repo
from orchestrator.search_port import SearchEngine, SearchOutcome, SearchUnavailable
from orchestrator.snapshots import load_snapshot, release_diff, segments_document
from verifier.readback import FlagSource, VerifyStatus, verify

log = logging.getLogger("trustgate.orchestrator")
ROLLBACK_PREFIX = "Rollback of"


@dataclass(frozen=True)
class DomainConfig:
    project: str
    environment: str
    control_identity: Identity  # outside the repair's segment, for read-back


def _evidence(candidate: RepairCandidate) -> str:
    v = candidate.validation
    if v is None or not v.runs:
        return "no validation runs"
    return (f"{v.failures} failures in {v.runs} runs; with 95% confidence the failure rate is "
            f"below about {3 / v.runs:.0%} (rule of three)")


def _explain(inv: Investigation, cand: RepairCandidate) -> str:
    """Plain-English summary written by code from structured results (LLM wording: later)."""
    culprits = ", ".join(c.flag for c in inv.culprit_set)
    what = cand.kind.value.replace("_", " ")
    if cand.kind is RepairKind.SEGMENT_OVERRIDE:
        what += f" on {inv.segment}"
    return (f"{culprits} changed in the release and reproduces the failure. "
            f"Proposed: {what}. Evidence: {_evidence(cand)}.")


class Orchestrator:
    def __init__(self, *, repo: Repo, gate: Any, search: SearchEngine,
                 configs: Mapping[Domain, DomainConfig],
                 flag_source: Callable[[str], FlagSource],
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 sleep: Callable[[float], None] = time.sleep, verify_timeout_s: float = 60.0,
                 llm: LLM | None = None, llm_model: str = ""):
        self.repo, self.gate, self.search = repo, gate, search
        self.configs, self.flag_source = configs, flag_source
        self.clock, self.sleep, self.verify_timeout_s = clock, sleep, verify_timeout_s
        # Optional: guided suspect ordering and the approver's explanation. Without it (or on
        # quota) the diff order and the code-written template are used.
        self.llm, self.llm_model = llm, llm_model

    def _save(self, inv: Investigation, status: InvestigationStatus | None = None) -> None:
        if status is not None:
            inv.status = status
        self.repo.put_investigation(inv)

    # -- investigation -----------------------------------------------------------------------

    def run(self, investigation_id: str) -> None:
        inv = self.repo.get_investigation(investigation_id)
        if inv is None:
            raise KeyError(investigation_id)
        try:
            self._run(inv)
        except SearchUnavailable as exc:
            log.warning("investigation %s: %s", inv.id, exc)
            self._save(inv, InvestigationStatus.INCONCLUSIVE)
        except Exception:
            log.exception("investigation %s failed", inv.id)
            self._save(inv, InvestigationStatus.FAILED)

    def _run(self, inv: Investigation) -> None:
        incident = self.repo.get_incident(inv.incident_id)
        cfg = self.configs[inv.domain]
        self._save(inv, InvestigationStatus.CAPTURING)

        baseline = load_snapshot(incident.baseline_snapshot, self.gate)
        release = load_snapshot(incident.release_snapshot, self.gate)
        ordering = order_suspects(release_diff(baseline, release), incident.trace, inv.mode,
                                  self.llm, model=self.llm_model)
        inv.suspects = ordering.suspects
        if ordering.note:
            log.info("investigation %s: %s", inv.id, ordering.note)
        if inv.suspects:  # a ranking exists (LLM, or diff order in plain mode / fallback)
            inv.timestamps.first_suspect = self.clock()
        view = self.gate.read("get_environment_document", {"environment": cfg.environment})
        try:
            inv.segment = triage(segments_document(view["view"]), incident.identity.traits)
        except TriageError as exc:
            log.warning("investigation %s: triage: %s", inv.id, exc)
            inv.segment = None
        self._save(inv, InvestigationStatus.SEARCHING)

        result = self.search.run(incident, inv.suspects, inv.mode)
        inv.steps, inv.culprit_set = result.steps, result.culprit_set
        inv.candidates = result.candidates
        inv.runs_used += result.runs_used
        inv.usd_used += result.usd_used
        if result.outcome is SearchOutcome.NOT_REPRODUCIBLE:
            return self._save(inv, InvestigationStatus.NOT_REPRODUCIBLE)
        if result.outcome is SearchOutcome.INCONCLUSIVE:
            return self._save(inv, InvestigationStatus.INCONCLUSIVE)
        self._save(inv, InvestigationStatus.VALIDATING)

        for cand in passing_candidates(result.candidates):
            try:
                ops = build_ops(cand.kind, inv.culprit_set, inv.suspects, segment=inv.segment,
                                project=cfg.project, environment=cfg.environment)
            except ProposalError as exc:  # e.g. no segment matched: try the next candidate
                log.info("investigation %s: skipping %s: %s", inv.id, cand.kind, exc)
                continue
            text = explain(culprit_set=inv.culprit_set, segment=inv.segment, chosen=cand,
                           options=result.candidates, llm=self.llm, model=self.llm_model,
                           fallback=_explain(inv, cand)).text
            proposal, calls = build_proposal(inv.id, cand, ops, self.gate,
                                             environment=cfg.environment, explanation=text,
                                             now=self.clock(), sleep=self.sleep)
            self.repo.put_proposal(proposal)
            self.repo.put_calls(proposal.id, calls)
            inv.proposal_id = proposal.id
            inv.timestamps.proposal = self.clock()
            return self._save(inv, InvestigationStatus.PROPOSED)
        self._save(inv, InvestigationStatus.INCONCLUSIVE)

    def reinvestigate(self, investigation_id: str) -> None:
        """After STALE: Flagsmith changed since the proposal, so start over from the snapshots."""
        inv = self.repo.get_investigation(investigation_id)
        if inv is None or inv.status is not InvestigationStatus.STALE:
            raise ValueError("only a STALE investigation can be re-investigated")
        inv.proposal_id = None
        self.run(investigation_id)

    # -- after the approved write ------------------------------------------------------------

    def verify_after_write(self, investigation_id: str) -> VerifyStatus:
        inv = self.repo.get_investigation(investigation_id)
        proposal = self.repo.get_proposal(inv.proposal_id or "")
        incident = self.repo.get_incident(inv.incident_id)
        cfg = self.configs[inv.domain]
        release_values = {c.flag: c.new for c in inv.suspects}
        expected = expectations(proposal.candidate.ops, incident.identity, cfg.control_identity,
                                release_values)
        result = verify(expected, self.flag_source(cfg.environment),
                        timeout_s=self.verify_timeout_s, sleep=self.sleep)
        proposal.readback = result.readbacks
        is_rollback = proposal.explanation.startswith(ROLLBACK_PREFIX)
        if result.status is VerifyStatus.VERIFIED:
            proposal.state = ProposalState.VERIFIED
            self.repo.put_proposal(proposal)
            if not is_rollback:
                inv.timestamps.verified = self.clock()
                self._save(inv, InvestigationStatus.VERIFIED)
            return result.status

        proposal.state = ProposalState.FAILED
        self.repo.put_proposal(proposal)
        if not is_rollback:
            self._propose_rollback(inv, proposal, release_values, cfg, result.detail)
        self._save(inv, InvestigationStatus.FAILED)
        return result.status

    def _propose_rollback(self, inv: Investigation, failed: Proposal,
                          release_values: dict[str, Any], cfg: DomainConfig, why: str) -> None:
        ops = inverse_ops(failed.candidate.ops, release_values)
        explanation = (f"{ROLLBACK_PREFIX} {failed.id}: read-back did not confirm the repair "
                       f"({why or 'no detail'}). This undoes it; it needs its own approval.")
        rollback, calls = build_proposal(inv.id, failed.candidate, ops, self.gate,
                                         environment=cfg.environment, explanation=explanation,
                                         now=self.clock(), sleep=self.sleep)
        self.repo.put_proposal(rollback)
        self.repo.put_calls(rollback.id, calls)
        inv.proposal_id = rollback.id
