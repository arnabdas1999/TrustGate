"""regressions.export: an investigation becomes a replayable regression case."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from contracts.models import Investigation, InvestigationMode, InvestigationStatus, Timestamps
from regressions.export import ExportError, build_case, export, load_cases, main
from sandbox.flags import effective_values
from tests.unit.test_search import SUSPECTS, incident, prompt_fast, synthetic_engine


def investigation(result, inc):
    return Investigation(id="inv1", incident_id=inc.id, domain=inc.domain,
                         mode=InvestigationMode.PLAIN, status=InvestigationStatus.PROPOSED,
                         suspects=SUSPECTS, culprit_set=result.culprit_set,
                         candidates=result.candidates,
                         timestamps=Timestamps(alert=datetime.now(UTC)))


@pytest.fixture
def found():
    inc = incident()
    result = synthetic_engine(prompt_fast).run(inc, SUSPECTS, InvestigationMode.PLAIN)
    return inc, investigation(result, inc)


def test_case_has_a_failing_and_a_repaired_config(found):
    inc, inv = found
    case = build_case(inc, inv)
    assert [c["flag"] for c in case["culprit"]] == ["agent_prompt_version"]
    assert case["repair_kind"] == "segment_override"
    failing = case["failing"]["values"]
    assert failing["agent_prompt_version"] == "v2_fast"  # the culprit stays on
    assert failing["agent_model"] == "model_flagship"  # everything else back to baseline
    from contracts.models import FlagConfig, Identity
    repaired = FlagConfig.model_validate(case["repaired"])
    served = effective_values(repaired, Identity.model_validate(case["identity"]))
    assert served["agent_prompt_version"] == "v1_strict"  # the override reaches tier 0
    assert prompt_fast(failing) and not prompt_fast(served)


def test_export_and_load(found, tmp_path):
    inc, inv = found
    path = export(inc, inv, tmp_path)
    assert path.name == f"{inc.id}.json"
    assert load_cases(tmp_path)[0]["from_investigation"] == "inv1"


def test_refuses_without_a_validated_repair(found):
    inc, inv = found
    with pytest.raises(ExportError, match="no validated repair"):
        build_case(inc, inv.model_copy(update={"candidates": []}))
    with pytest.raises(ExportError, match="no culprit"):
        build_case(inc, inv.model_copy(update={"culprit_set": []}))


def test_cli(found, tmp_path, capsys):
    inc, inv = found
    (tmp_path / "inv.json").write_text(inv.model_dump_json())
    (tmp_path / "inc.json").write_text(inc.model_dump_json())
    out = tmp_path / "cases"
    assert main(["--investigation", str(tmp_path / "inv.json"), "--incident",
                 str(tmp_path / "inc.json"), "--out", str(out)]) == 0
    assert json.loads((out / f"{inc.id}.json").read_text())["domain"] == "opspilot"
