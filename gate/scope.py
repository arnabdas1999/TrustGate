"""What the gate is allowed to touch: one Flagsmith project and a named set of environments."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GateScope:
    project_id: int
    project_name: str
    # Environment name -> Flagsmith environment id. Writes and scoped reads must stay inside it.
    environments: dict[str, int] = field(default_factory=dict)

    def environment_ids(self) -> set[str]:
        return {str(i) for i in self.environments.values()}

    def allows_project(self, project: object) -> bool:
        allowed = {str(self.project_id)}
        if self.project_name.strip():  # a blank name must never match a blank project
            allowed.add(self.project_name)
        return str(project) in allowed

    def allows_environment(self, environment: object) -> bool:
        return str(environment) in self.environment_ids() | set(self.environments)
