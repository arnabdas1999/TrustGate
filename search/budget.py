"""Run and dollar caps for one investigation. Every replay is charged before its result counts."""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExhausted(Exception):
    pass


@dataclass
class Budget:
    max_runs: int = 160
    max_usd: float = 5.0
    runs: int = 0
    usd: float = 0.0

    def can_afford(self, n: int) -> bool:
        return self.runs + n <= self.max_runs and self.usd < self.max_usd

    def charge(self, runs: int, usd: float) -> None:
        self.runs += runs
        self.usd = round(self.usd + usd, 6)

    @property
    def exhausted(self) -> bool:
        return self.runs >= self.max_runs or self.usd >= self.max_usd

    def remaining_runs(self) -> int:
        return max(0, self.max_runs - self.runs)
