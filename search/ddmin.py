"""Delta debugging (Zeller's ddmin) over the release's flag changes.

`test(subset)` applies only `subset` of the release on top of the baseline and returns
FAIL (the failure reproduced), PASS, or INCONCLUSIVE. ddmin returns a 1-minimal
failure-inducing set: removing any single change from it makes the failure disappear (as far
as the tests could tell). It is not necessarily the globally smallest set or the unique
cause, and never "the root cause".

Suspect order matters: subsets are cut from the list in the order given, so guided ordering
(most likely first) reaches a single culprit in fewer tests. Results are memoised per subset.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from contracts.models import TestOutcome


@dataclass
class DDResult[T]:
    culprit: list[T]
    outcome: TestOutcome  # FAIL: culprit found; INCONCLUSIVE: a test could not be decided
    tests: int = 0
    log: list[tuple[list[T], TestOutcome]] = field(default_factory=list)


def _split[T](items: list[T], n: int) -> list[list[T]]:
    size, extra = divmod(len(items), n)
    out, start = [], 0
    for i in range(n):
        end = start + size + (1 if i < extra else 0)
        out.append(items[start:end])
        start = end
    return [s for s in out if s]


def ddmin[T](changes: Sequence[T], test: Callable[[list[T]], TestOutcome]) -> DDResult[T]:
    """`changes` must fail as a whole (the caller reproduced it first). Works on positions, so
    the items need not be hashable or unique."""
    memo: dict[frozenset[int], TestOutcome] = {}
    items = list(changes)
    result: DDResult[T] = DDResult(culprit=items, outcome=TestOutcome.FAIL)

    def run(subset: list[int]) -> TestOutcome:
        k = frozenset(subset)
        if k not in memo:
            picked = [items[i] for i in subset]
            memo[k] = test(picked)
            result.tests += 1
            result.log.append((picked, memo[k]))
        return memo[k]

    current = list(range(len(items)))
    n = 2
    undecided = False
    while len(current) >= 2:
        subsets = _split(current, n)
        reduced = False
        for subset in subsets:  # 1. reduce to a subset
            outcome = run(subset)
            if outcome is TestOutcome.FAIL:
                current, n, reduced = subset, 2, True
                break
            undecided |= outcome is TestOutcome.INCONCLUSIVE
        if not reduced and n > 2:  # 2. reduce to a complement (n == 2 repeats step 1)
            for subset in subsets:
                complement = [c for c in current if c not in subset]
                outcome = run(complement)
                if outcome is TestOutcome.FAIL:
                    current, n, reduced = complement, max(n - 1, 2), True
                    break
                undecided |= outcome is TestOutcome.INCONCLUSIVE
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)  # 3. finer granularity
    result.culprit = [items[i] for i in current]
    if undecided and len(current) > 1:
        # An undecided test may have hidden a smaller failing set: say so, don't guess.
        result.outcome = TestOutcome.INCONCLUSIVE
    return result
