"""Stretch #2: turn a finished investigation into a regression test.

Each export writes regressions/cases/<incident_id>.json with two replay configurations:
- failing: the baseline plus only the culprit changes (the 1-minimal set). It must still break
  the agent, otherwise the regression no longer reproduces.
- repaired: the configuration the chosen repair was validated with. It must stay clean.

tests/llm/test_regressions.py replays every exported case on the real model (pytest -m llm),
so a later prompt, tool or model change that brings the failure back, or breaks the fix, is
caught before release.

    uv run python -m regressions.export --investigation data/inv.json --incident data/inc.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from contracts.models import Incident, Investigation, RepairCandidate, TestOutcome
from search.engine import apply_subset

CASES_DIR = Path(__file__).parent / "cases"


class ExportError(Exception):
    pass


def chosen(inv: Investigation) -> RepairCandidate:
    """The narrowest candidate that passed validation, as the orchestrator proposes it."""
    from orchestrator.proposals import choose_candidate

    cand = choose_candidate(inv.candidates)
    if cand is None or cand.validation is None:
        raise ExportError(f"{inv.id}: no validated repair to export")
    return cand


def build_case(incident: Incident, inv: Investigation) -> dict[str, Any]:
    if not inv.culprit_set:
        raise ExportError(f"{inv.id}: no culprit set (status {inv.status})")
    candidate = chosen(inv)
    captured = dict(incident.resolved_flags.values)
    failing = apply_subset(captured, inv.suspects, inv.culprit_set)
    repaired = candidate.validation.config
    if candidate.validation.outcome is not TestOutcome.PASS:
        raise ExportError(f"{inv.id}: the chosen repair did not pass validation")
    return {
        "id": incident.id,
        "domain": incident.domain.value,
        "case_id": incident.case_id,
        "identity": incident.identity.model_dump(mode="json"),
        "culprit": [c.model_dump(mode="json") for c in inv.culprit_set],
        "repair_kind": candidate.kind.value,
        "failing": failing.model_dump(mode="json"),
        "repaired": repaired.model_dump(mode="json"),
        "from_investigation": inv.id,
    }


def export(incident: Incident, inv: Investigation, out_dir: Path = CASES_DIR) -> Path:
    case = build_case(incident, inv)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{case['id']}.json"
    path.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                    newline="\n")
    return path


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(cases_dir.glob("*.json"))]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--investigation", type=Path, required=True, help="Investigation JSON")
    p.add_argument("--incident", type=Path, required=True, help="Incident JSON")
    p.add_argument("--out", type=Path, default=CASES_DIR)
    args = p.parse_args(argv)
    inv = Investigation.model_validate_json(args.investigation.read_text(encoding="utf-8"))
    incident = Incident.model_validate_json(args.incident.read_text(encoding="utf-8"))
    try:
        path = export(incident, inv, args.out)
    except ExportError as exc:
        print(f"not exported: {exc}", file=sys.stderr)
        return 1
    print(f"exported {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
