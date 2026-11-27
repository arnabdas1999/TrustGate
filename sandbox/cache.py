"""Replay cache, keyed by (config, case, seed, model IDs, prompt hash). DISPLAY ONLY.

Cached runs may be replayed in the UI and the video. Confirmation, repair validation and
post-write runs are always fresh and never read from here (frozen plan, tiered testing).
Everything read back is marked fresh=False.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from contracts.models import FlagConfig, GradeResult, Trace

DEFAULT_DIR = Path("data/cache")


@dataclass
class CachedRun:
    trace: Trace
    grade: GradeResult
    fresh: bool = False  # always False: a cached run is never evidence


def cache_key(config: FlagConfig, case_id: str, seed: int, *, model: str,
              prompt_hash: str) -> str:
    payload = json.dumps({"values": config.values, "segment_overrides": config.segment_overrides,
                          "case": case_id, "seed": seed, "model": model,
                          "prompt": prompt_hash}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class DisplayCache:
    def __init__(self, root: Path = DEFAULT_DIR):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def put(self, key: str, trace: Trace, grade: GradeResult) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"display_only": True, "trace": trace.model_dump(mode="json"),
                                    "grade": grade.model_dump(mode="json")}), encoding="utf-8")

    def get(self, key: str) -> CachedRun | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        grade = {k: v for k, v in data["grade"].items() if k != "passed"}
        return CachedRun(trace=Trace.model_validate(data["trace"]),
                         grade=GradeResult.model_validate(grade))
