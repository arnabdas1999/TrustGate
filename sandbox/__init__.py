"""Sandboxes for the demo agents (Khyati). See sandbox/adapter.py for the protocol."""

from __future__ import annotations

from contracts.models import Domain
from sandbox.adapter import SqliteDomain, TargetAppAdapter


def get_adapter(domain: Domain | str, **kwargs) -> SqliteDomain:
    domain = Domain(domain)
    if domain == Domain.OPSPILOT:
        from sandbox.opspilot import OpsPilotAdapter
        return OpsPilotAdapter(**kwargs)
    from sandbox.vaultguard import VaultGuardAdapter
    return VaultGuardAdapter(**kwargs)


def evaluate_global_rollback(incident, release: dict, *, adapter=None,
                             runs_per_case: int = 3) -> tuple[int, int, float]:
    """bench/run_arms.py's global_rollback arm: revert every release change (sandbox only) and
    score the held-out cases. Returns (heldout_passes, heldout_runs, cost_kept); a full
    rollback keeps none of the release's cost saving, so cost_kept is 0.0."""
    from contracts.models import ConfigSource, FlagConfig
    from sandbox.flags import BASELINE
    from sandbox.runner import run_batch_sync

    adapter = adapter or get_adapter(incident.domain)
    values = dict(incident.resolved_flags.values)
    for change in release.get("changes", []):
        flag = change["flag"]
        old = change.get("old", BASELINE.get(flag))
        if old is None:
            values.pop(flag, None)
        else:
            values[flag] = old
    cfg = FlagConfig(values=values, source=ConfigSource.OVERRIDE)
    passes = runs = 0
    for case_id in adapter.case_ids("heldout"):
        batch = run_batch_sync(adapter, case_id, cfg, runs_per_case)
        passes += batch.passes
        runs += len(batch.completed)
    return passes, runs, 0.0


__all__ = ["TargetAppAdapter", "evaluate_global_rollback", "get_adapter"]
