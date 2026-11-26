"""Plain-English explanation of a proposal, for the approver.

Built ONLY from structured results: the culprit set, the segment, the evidence numbers and the
repair options with their cost kept. No trace text, descriptions or audit entries go into the
prompt. The output is display text only. It never reaches a WriteOp or payload; code builds
those. Without an LLM (quota, disabled) or on an empty answer, the code-written template is used.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent.llm import LLM, LLMUnavailable
from agent.ordering import data_block
from contracts.models import FlagChange, RepairCandidate

MAX_TOKENS = 200
MAX_CHARS = 600


@dataclass
class Explanation:
    text: str
    source: str  # "llm" or "template"
    tokens_in: int = 0
    tokens_out: int = 0


def _option(c: RepairCandidate) -> dict[str, Any]:
    v = c.validation
    return {"kind": c.kind.value, "cost_kept": c.cost_kept, "violations": c.violations,
            "validation_runs": v.runs if v else None,
            "validation_failures": v.failures if v else None,
            "heldout_passes": c.heldout_passes, "heldout_runs": c.heldout_runs}


def facts(culprit_set: Sequence[FlagChange], segment: str | None, chosen: RepairCandidate,
          options: Sequence[RepairCandidate]) -> dict[str, Any]:
    v = chosen.validation
    return {
        "culprits": [{"flag": c.flag, "old": c.old, "new": c.new} for c in culprit_set],
        "segment": segment,
        "chosen": chosen.kind.value,
        "evidence": {"validation_runs": v.runs if v else None,
                     "validation_failures": v.failures if v else None},
        "options": [_option(o) for o in options],
    }


def explain(*, culprit_set: Sequence[FlagChange], segment: str | None, chosen: RepairCandidate,
            options: Sequence[RepairCandidate], llm: LLM | None, model: str,
            fallback: str) -> Explanation:
    if llm is None:
        return Explanation(fallback, "template")
    prompt = (
        "Write two or three short, plain sentences for the person approving a feature-flag "
        "repair. Use only the facts block below; do not invent numbers or "
        "flags. Say which flag change caused the failure, what the chosen repair does and "
        "what it keeps compared with the other options.\n"
        f"<facts>{data_block(facts(culprit_set, segment, chosen, options))}</facts>"
    )
    try:
        out = llm.complete(prompt, model=model, max_tokens=MAX_TOKENS)
    except Exception:  # noqa: BLE001 (quota, network, anything: the template is always fine)
        return Explanation(fallback, "template")
    text = re.sub(r"\s+", " ", out.text).strip()[:MAX_CHARS].strip()
    if not text:
        return Explanation(fallback, "template", out.tokens_in, out.tokens_out)
    return Explanation(text, "llm", out.tokens_in, out.tokens_out)


__all__ = ["Explanation", "LLMUnavailable", "explain", "facts"]
