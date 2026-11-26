"""Search (Khyati): ddmin over release changes, tiered testing, repair validation.

    from search import SandboxSearch, run
    run(incident, suspects, mode) -> orchestrator.search_port.SearchResult
"""

from search.engine import SandboxSearch, run

__all__ = ["SandboxSearch", "run"]
