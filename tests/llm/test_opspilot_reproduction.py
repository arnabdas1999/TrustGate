"""CP2 reproduction gate on the real LLM (spends credits: uv run --env-file .env pytest -m llm).

Release config fails the hero in at least 8 of 10 runs; baseline fails 0 of 10. Errored runs
(provider down, timeouts) make the test fail rather than count as passes.
"""

from __future__ import annotations

import os

import pytest

from sandbox import get_adapter
from sandbox.flags import baseline_config, release_config
from sandbox.runner import run_batch_sync

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def ops():
    if not os.environ.get("LLM_API_KEY") or not os.environ.get("MODEL_CHEAP"):
        pytest.skip("LLM_API_KEY / MODEL_CHEAP / MODEL_FLAGSHIP not set")
    return get_adapter("opspilot")


def test_release_reproduces(ops):
    batch = run_batch_sync(ops, "hero", release_config(), 10)
    assert batch.errors == 0, [r.trace.error for r in batch.runs if r.trace.error]
    assert batch.failures >= 8, [r.grade.evidence["tool_sequence"] for r in batch.runs]


def test_baseline_is_clean(ops):
    batch = run_batch_sync(ops, "hero", baseline_config(), 10)
    assert batch.errors == 0
    assert batch.failures == 0, [r.grade.violations for r in batch.runs]
    assert batch.passes == 10, [r.grade.evidence for r in batch.runs if not r.grade.passed]
