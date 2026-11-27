"""Replays every exported regression case on the real model (pytest -m llm).

The failing configuration (baseline + the culprit) must still break the agent, and the
configuration the repair was validated with must stay clean. See regressions/export.py.
"""

from __future__ import annotations

import os

import pytest

from contracts.models import FlagConfig, Identity
from regressions.export import load_cases
from sandbox import get_adapter
from sandbox.runner import run_batch_sync

pytestmark = pytest.mark.llm
RUNS = 3
CASES = load_cases()


@pytest.fixture(scope="module")
def adapters():
    if not os.environ.get("MODEL_CHEAP"):
        pytest.skip("MODEL_CHEAP / MODEL_FLAGSHIP not set")
    return {}


@pytest.mark.skipif(not CASES, reason="no exported regression cases yet")
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_regression(case, adapters):
    adapter = adapters.setdefault(case["domain"], get_adapter(case["domain"]))
    identity = Identity.model_validate(case["identity"])
    failing = run_batch_sync(adapter, case["case_id"], FlagConfig.model_validate(case["failing"]),
                             RUNS, identity=identity, early_stop=True)
    assert failing.failures >= 1, "the culprit no longer breaks the agent"
    repaired = run_batch_sync(adapter, case["case_id"],
                              FlagConfig.model_validate(case["repaired"]), RUNS,
                              identity=identity)
    assert repaired.errors == 0
    assert repaired.failures == 0, [r.grade.violations for r in repaired.runs]
