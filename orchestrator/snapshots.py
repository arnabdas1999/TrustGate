"""Snapshots and the release diff: the old and new value of every flag the release changed.

An incident names two snapshots by reference ("path or ref", contracts.Incident):
- "file:<path>", or a bare path: a JSON file, either {"values": {flag: value}} (what
  scripts/release.py saves, typed values from the admin document) or a flat {flag: value}
- "gate:<environment>": the environment as it is now, read through the gate
  (get_environment_document, an allowlisted read)
The diff between them is the suspect list. The audit log is never parsed for values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contracts.models import FlagChange


class SnapshotError(Exception):
    pass


def _file_values(path: str) -> dict[str, Any]:
    try:
        body = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"can't read snapshot {path}: {type(exc).__name__}") from None
    values = body.get("values", body) if isinstance(body, dict) else None
    if not isinstance(values, dict) or not all(
            v is None or isinstance(v, (str, int, float, bool)) for v in values.values()):
        raise SnapshotError(f"snapshot {path} is not {{flag: value}} or {{\"values\": ...}}")
    return values


def load_snapshot(ref: str, gate: Any) -> dict[str, Any]:
    scheme, _, rest = ref.partition(":")
    if scheme == "gate":
        if not rest:
            raise SnapshotError(f"bad snapshot reference {ref!r}")
        view = gate.read("get_environment_document", {"environment": rest})["view"]
        return {name: state["value"] for name, state in view["features"].items()}
    path = rest if scheme == "file" else ref  # anything else is a path
    if not path:
        raise SnapshotError(f"bad snapshot reference {ref!r}")
    return _file_values(path)


def same_value(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b or (a is not None and b is not None and str(a) == str(b))


def _typed_like(value: Any, like: Any) -> Any:
    """The edge returns ints as strings ("12"): give the value the type of the typed side, so
    ops and rollbacks write 12, not "12"."""
    if isinstance(value, str) and not isinstance(like, bool) and isinstance(like, (int, float)):
        try:
            number = float(value)
        except ValueError:
            return value
        if isinstance(like, int) and number.is_integer():
            return int(number)
        return number if isinstance(like, float) else value
    return value


def release_diff(baseline: dict[str, Any], release: dict[str, Any]) -> list[FlagChange]:
    """Changed flags, by name. "6" and 6 are the same value (the edge returns ints as strings)."""
    return [FlagChange(flag=f, old=baseline.get(f),
                       new=_typed_like(release.get(f), baseline.get(f)))
            for f in sorted(set(baseline) | set(release))
            if not same_value(baseline.get(f), release.get(f))]


def segments_document(view: dict[str, Any]) -> dict[str, Any]:
    """The gate's canonical view, reshaped for agent.triage."""
    return {"project": {"segments": [{"name": name, "rules": seg["rules"]}
                                     for name, seg in sorted(view["segments"].items())]}}
