"""Sandboxes for the demo agents (Khyati). See sandbox/adapter.py for the protocol."""

from __future__ import annotations

from contracts.models import Domain
from sandbox.adapter import SqliteDomain, TargetAppAdapter


def get_adapter(domain: Domain | str, **kwargs) -> SqliteDomain:
    domain = Domain(domain)
    if domain == Domain.OPSPILOT:
        from sandbox.opspilot import OpsPilotAdapter
        return OpsPilotAdapter(**kwargs)
    raise NotImplementedError(f"{domain} sandbox lands on Friday (CP6)")


__all__ = ["TargetAppAdapter", "get_adapter"]
