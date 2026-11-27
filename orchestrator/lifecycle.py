"""Investigation lifecycle: capture -> suspects -> search -> candidates -> proposal, then the
verifier after the approved write.

Status flow: CAPTURING -> SEARCHING -> VALIDATING -> PROPOSED -> (API: APPROVED/APPLIED)
-> VERIFIED, or FAILED with a code-built rollback proposal that needs its own approval.
Exits: NOT_REPRODUCIBLE, INCONCLUSIVE, STALE (-> reinvestigate). Timestamps go into
Investigation.timestamps: alert (API), first_suspect (search isolated the culprit), proposal,
approved (API), verified.

After a failed read-back the investigation stays FAILED: the rollback proposal (id
"<original>_rollback") gets VERIFIED or FAILED itself, and original_proposal_id() recovers the
repair's id from it.

Nothing here touches Flagsmith directly: reads and fingerprints go through the gate, and
writes only happen when the API sends a signed approval to the gate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
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
    WriteOp,
)
from orchestrator.proposals import (
    ProposalError,
    build_proposal,
    check_ops_match_validation,
    expectations,
    inverse_ops,
    ops_for_candidate,
    passing_candidates,
)
from orchestrator.repo import Repo
from orchestrator.search_port import SearchEngine, SearchOutcome, SearchUnavailable
from orchestrator.snapshots import (
    load_snapshot,
    release_diff,
    same_segment_rules,
    segments_document,
)
from verifier.readback import FlagSource, VerifyStatus, verify

log = logging.getLogger("trustgate.orchestrator")
ROLLBACK_PREFIX = "Rollback of"
ROLLBACK_SUFFIX = "_rollback"


def is_rollback(proposal_id: str) -> bool:
    """By id, never by explanation text (an LLM-written explanation could start with anything)."""
    return proposal_id.endswith(ROLLBACK_SUFFIX)


def original_proposal_id(proposal_id: str) -> str:
    """The repair a rollback undoes (the id itself for a repair)."""
    return proposal_id.removesuffix(ROLLBACK_SUFFIX)


@dataclass(frozen=True)
class DomainConfig:
    project: str
    environment: str
    control_identity: Identity  # outside the repair's segment, for read-back


def _fallback_evidence_line(failures: int, runs: int, distinct: int | None = None) -> str:
    """Same wording as search.testing.evidence_line, for when search/ isn't importable."""
    if failures:
        return f"{failures} failures in {runs} runs"
    if distinct == 1 and runs > 1:
        return (f"0 failures in {runs} runs, all identical (the agent runs at temperature 0), "
                "so this is one behaviour observed, not a failure-rate estimate")
    line = (f"0 failures in {runs} runs. With 95% confidence the failure rate is below "
            f"about {3.0 / runs:.0%}")
    if distinct is not None:
        line += f" ({distinct} distinct behaviours)"
    return line


def _evidence_line() -> Callable[[int, int, int | None], str]:
    """search.testing.evidence_line when search/ is importable (lazily, like search_port)."""
    try:
        from search.testing import evidence_line  # noqa: PLC0415
    except Exception:  # noqa: BLE001 (search/ missing or broken: use the same wording here)
        return _fallback_evidence_line
    return evidence_line


def _evidence(candidate: RepairCandidate) -> str:
    """The rule of three only when the runs differed: identical runs at temperature 0 are one
    behaviour observed, not a failure-rate estimate."""
    v = candidate.validation
    if v is None or not v.runs:
        return "no validation runs"
    return _evidence_line()(v.failures, v.runs, v.distinct_behaviours)


def _join(names: Sequence[str]) -> str:
    """a / a and b / a, b and c"""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _explain(inv: Investigation, cand: RepairCandidate, ops: Sequence[WriteOp]) -> str:
    """Plain-English summary written by code from structured results: which flags reproduce
    the failure, which of them the repair reverts, where, and which it keeps."""
    culprits = sorted(c.flag for c in inv.culprit_set)
    if len(culprits) == 1:
        cause = f"{culprits[0]} changed in the release and reproduces the failure"
    else:
        cause = f"{_join(culprits)} together reproduce the failure"
    reverted = sorted(dict.fromkeys(op.feature for op in ops))
    kept = [f for f in culprits if f not in reverted]
    if cand.kind is RepairKind.GLOBAL:
        repair = f"the repair reverts every release change ({_join(reverted)}) for everyone"
    else:
        where = f" on {inv.segment}" if cand.kind is RepairKind.SEGMENT_OVERRIDE else \
            " for everyone"
        repair = f"the repair reverts {_join(reverted)}{where}"
        if kept:
            repair += f" and keeps {_join(kept)}"
    return f"{cause}; {repair}. Evidence: {_evidence(cand)}."


class Orchestrator:
    def __init__(self, *, repo: Repo, gate: Any, search: SearchEngine,
                 configs: Mapping[Domain, DomainConfig],
                 flag_source: Callable[[str], FlagSource],
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 sleep: Callable[[float], None] = time.sleep, verify_timeout_s: float = 60.0,
                 llm: LLM | None = None, llm_model: str = "",
                 replay_segments: Callable[[], Mapping[str, Any] | None] | None = None):
        self.repo, self.gate, self.search = repo, gate, search
        self.configs, self.flag_source = configs, flag_source
        self.clock, self.sleep, self.verify_timeout_s = clock, sleep, verify_timeout_s
        # Optional: guided suspect ordering and the approver's explanation. Without it (or on
        # quota) the diff order and the code-written template are used.
        self.llm, self.llm_model = llm, llm_model
        # The segment definitions the search replays against (the sandbox's copy). When set,
        # the triaged segment's live Flagsmith rules must match them, or the search would
        # validate a repair for a different set of identities than the write reaches.
        self.replay_segments = replay_segments

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
        view = self.gate.read("get_environment_document", {"environment": cfg.environment})
        try:
            inv.segment = triage(segments_document(view["view"]), incident.identity.traits)
        except TriageError as exc:
            log.warning("investigation %s: triage: %s", inv.id, exc)
            inv.segment = None
        mismatch = self._segment_mismatch(inv.segment, view["view"])
        if mismatch:
            log.warning("investigation %s: inconclusive: %s", inv.id, mismatch)
            return self._save(inv, InvestigationStatus.INCONCLUSIVE)
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
        if inv.culprit_set:  # time to first suspect = when the search isolated the culprit
            inv.timestamps.first_suspect = result.culprit_found_at or self.clock()
        self._save(inv, InvestigationStatus.VALIDATING)

        for cand in passing_candidates(result.candidates):
            try:
                ops = ops_for_candidate(cand, inv.culprit_set, inv.suspects,
                                        segment=inv.segment, project=cfg.project,
                                        environment=cfg.environment)
                check_ops_match_validation(ops, cand, inv.suspects)
            except ProposalError as exc:  # no segment, or not what was validated: next one
                log.info("investigation %s: skipping %s: %s", inv.id, cand.kind, exc)
                continue
            text = explain(culprit_set=inv.culprit_set, segment=inv.segment, chosen=cand,
                           options=result.candidates, llm=self.llm, model=self.llm_model,
                           fallback=_explain(inv, cand, ops)).text
            proposal, calls = build_proposal(inv.id, cand, ops, self.gate,
                                             environment=cfg.environment, explanation=text,
                                             now=self.clock(), sleep=self.sleep)
            self.repo.put_proposal(proposal)
            self.repo.put_calls(proposal.id, calls)
            inv.proposal_id = proposal.id
            inv.timestamps.proposal = self.clock()
            return self._save(inv, InvestigationStatus.PROPOSED)
        self._save(inv, InvestigationStatus.INCONCLUSIVE)

    def _segment_mismatch(self, segment: str | None, view: dict[str, Any]) -> str | None:
        """A reason when the replay's copy of the triaged segment differs from Flagsmith's."""
        if self.replay_segments is None or not segment:
            return None
        replay = self.replay_segments()
        if replay is None:
            return None  # no sandbox copy (e.g. scripted search): nothing to compare
        ours = replay.get(segment)
        live = (view.get("segments") or {}).get(segment, {}).get("rules")
        if ours is None:
            return f"segment {segment} exists in Flagsmith but not in the sandbox"
        if live is None or not same_segment_rules(live, ours.get("rules") or []):
            return f"segment rules differ between Flagsmith and the sandbox ({segment})"
        return None

    def reinvestigate(self, investigation_id: str) -> None:
        """After STALE: Flagsmith changed since the proposal, so start over from the snapshots."""
        inv = self.repo.get_investigation(investigation_id)
        if inv is None or inv.status is not InvestigationStatus.STALE:
            raise ValueError("only a STALE investigation can be re-investigated")
        inv.proposal_id = None
        self.run(investigation_id)

    # -- after the approved write ------------------------------------------------------------

    def verify_after_write(self, investigation_id: str) -> VerifyStatus:
        """Runs after the gate has written. Never leaves the investigation in APPLIED: any
        failure (missing or wrong ser. key, gate 503, ...) ends in FAILED, and a failed repair
        gets a rollback proposal, built in its own try block."""
        inv = self.repo.get_investigation(investigation_id)
        if inv is None:
            raise KeyError(investigation_id)
        proposal = self.repo.get_proposal(inv.proposal_id or "")
        if proposal is None:
            log.error("verify %s: no proposal %r", inv.id, inv.proposal_id)
            self._save(inv, InvestigationStatus.FAILED)
            return VerifyStatus.FAILED
        rollback = is_rollback(proposal.id)
        try:
            result = self._read_back(inv, proposal)
            status, detail = result.status, result.detail
            proposal.readback = result.readbacks
        except Exception as exc:  # noqa: BLE001 (the write already happened: never stay APPLIED)
            log.exception("verify %s: read-back could not run", inv.id)
            status, detail = VerifyStatus.FAILED, f"read-back could not run: {type(exc).__name__}"

        if status is VerifyStatus.VERIFIED:
            proposal.state = ProposalState.VERIFIED
            self.repo.put_proposal(proposal)
            if rollback:  # the repair failed and is now undone: the investigation stays FAILED
                self._save(inv, InvestigationStatus.FAILED)
            else:
                inv.timestamps.verified = self.clock()
                self._save(inv, InvestigationStatus.VERIFIED)
            return status

        if proposal.state is ProposalState.APPLIED:
            proposal.state = ProposalState.FAILED
        self.repo.put_proposal(proposal)
        if not rollback:
            try:
                self._propose_rollback(inv, proposal, detail)
            except Exception:  # noqa: BLE001 (e.g. gate 503): FAILED still shows, no rollback card
                log.exception("verify %s: could not propose a rollback", inv.id)
        self._save(inv, InvestigationStatus.FAILED)
        return status

    def _read_back(self, inv: Investigation, proposal: Proposal) -> Any:
        incident = self.repo.get_incident(inv.incident_id)
        cfg = self.configs[inv.domain]
        expected = expectations(proposal.candidate.ops, incident.identity, cfg.control_identity,
                                {c.flag: c.new for c in inv.suspects})
        return verify(expected, self.flag_source(cfg.environment),
                      timeout_s=self.verify_timeout_s, sleep=self.sleep)

    def _propose_rollback(self, inv: Investigation, failed: Proposal, why: str) -> None:
        cfg = self.configs[inv.domain]
        ops = inverse_ops(failed.candidate.ops, {c.flag: c.new for c in inv.suspects})
        explanation = (f"{ROLLBACK_PREFIX} {failed.id}: read-back did not confirm the repair "
                       f"({why or 'no detail'}). This undoes it; it needs its own approval.")
        rollback, calls = build_proposal(inv.id, failed.candidate, ops, self.gate,
                                         environment=cfg.environment, explanation=explanation,
                                         now=self.clock(), sleep=self.sleep,
                                         proposal_id=failed.id + ROLLBACK_SUFFIX)
        self.repo.put_proposal(rollback)
        self.repo.put_calls(rollback.id, calls)
        inv.proposal_id = rollback.id  # the UI shows the card to approve; the id names the repair
