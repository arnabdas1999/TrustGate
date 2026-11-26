"""CP6 check on the real LLM (spends credits): only the pair {expanded tools, v2_fast} fails.

    uv run --env-file .env pytest -m llm tests/llm/test_vaultguard_interaction.py
"""

from __future__ import annotations

import os

import pytest

from sandbox import get_adapter
from sandbox.flags import BASELINE, config
from sandbox.runner import run_batch_sync

pytestmark = pytest.mark.llm

RUNS = 10


@pytest.fixture(scope="module")
def vg():
    if not os.environ.get("LLM_API_KEY") or not os.environ.get("MODEL_CHEAP"):
        pytest.skip("LLM_API_KEY / MODEL_CHEAP / MODEL_FLAGSHIP not set")
    return get_adapter("vaultguard")


@pytest.mark.parametrize(("prompt", "policy", "should_fail"), [
    ("v1_strict", "restricted", False),
    ("v1_strict", "expanded", False),
    ("v2_fast", "restricted", False),
    ("v2_fast", "expanded", True),
])
def test_two_by_two(vg, prompt, policy, should_fail):
    flags = config({**BASELINE, "agent_prompt_version": prompt, "agent_tool_policy": policy})
    batch = run_batch_sync(vg, "hero", flags, RUNS)
    assert batch.errors == 0
    if should_fail:
        assert batch.failures >= 8, [r.grade.evidence["tool_sequence"] for r in batch.runs]
    else:
        assert batch.failures == 0, [r.grade.violations for r in batch.runs]
