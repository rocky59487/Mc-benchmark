"""Run planning: execution order, repetition, and factorial designs.

Implements docs/METHODOLOGY.md sections 3 and 6.

The planner is small but it is where the fairness guarantee is actually made.
Everything else measures; this decides *when* each thing is measured, and that
ordering is what separates a real effect from thermal drift.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Sequence

__all__ = [
    "OrderStrategy",
    "Cell",
    "PlannedRun",
    "RunPlan",
    "plan_runs",
    "factorial_variants",
]

MIN_RUNS_PER_CELL = 5
DEFAULT_RUNS_PER_CELL = 7
MAX_FACTORIAL_FACTORS = 4


class OrderStrategy(str, Enum):
    """How runs are ordered across variants.

    ``INTERLEAVED`` is the default and the only strategy admissible for
    published results. The others exist for debugging and for demonstrating,
    with real numbers, why they are wrong.
    """

    INTERLEAVED = "interleaved"
    BLOCKED = "blocked"
    RANDOM = "random"


@dataclass(frozen=True)
class Cell:
    """One (scenario, variant) pair — the unit that gets repeated."""

    scenario: str
    variant: str

    def __str__(self) -> str:
        return f"{self.scenario}/{self.variant}"


@dataclass(frozen=True)
class PlannedRun:
    """A single scheduled run.

    ``position`` is the index in the global execution order and is retained in
    results so that order effects remain auditable after the fact: if run
    position correlates with the metric within a variant, the environment
    drifted and the suite should be treated with suspicion.
    """

    cell: Cell
    replicate: int
    position: int
    round_index: int


@dataclass
class RunPlan:
    """A complete, ordered execution schedule."""

    runs: list[PlannedRun]
    strategy: OrderStrategy
    seed: int
    runs_per_cell: int
    cells: list[Cell] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.runs)

    def __iter__(self) -> Iterator[PlannedRun]:
        return iter(self.runs)

    def for_cell(self, cell: Cell) -> list[PlannedRun]:
        return [r for r in self.runs if r.cell == cell]

    def positions_by_variant(self) -> dict[str, list[int]]:
        """Execution positions per variant, for post-hoc order-effect checks."""
        out: dict[str, list[int]] = {}
        for run in self.runs:
            out.setdefault(run.cell.variant, []).append(run.position)
        return out


def plan_runs(
    scenarios: Sequence[str],
    variants: Sequence[str],
    *,
    runs_per_cell: int = DEFAULT_RUNS_PER_CELL,
    strategy: OrderStrategy = OrderStrategy.INTERLEAVED,
    seed: int = 0,
) -> RunPlan:
    """Build the execution schedule for a suite.

    The default interleaved strategy runs one replicate of every variant per
    round, shuffling the variant order within each round:

        round 1:  B  A  C
        round 2:  A  C  B
        round 3:  C  B  A

    rather than the blocked ``AAAAA BBBBB CCCCC`` that every existing Minecraft
    benchmark uses. Blocked execution confounds variant with wall-clock time,
    and time carries thermal throttling, page-cache warming, background load,
    and ambient temperature drift. On a machine that throttles after ten
    minutes, blocked ordering hands a clean, repeatable, entirely fake win to
    whichever variant ran first — and it will reproduce, which is what makes it
    so dangerous. Interleaving converts that systematic bias into noise the
    statistics can see and price in.

    Scenarios are held together within a round rather than interleaved with each
    other, because switching scenario means regenerating a world; interleaving
    at that granularity would dominate the schedule with setup cost while
    controlling for a drift that operates over much longer timescales.

    ``seed`` is recorded on the plan so the exact order can be reproduced.
    """
    if not scenarios:
        raise ValueError("plan_runs() requires at least one scenario")
    if not variants:
        raise ValueError("plan_runs() requires at least one variant")
    if len(set(variants)) != len(variants):
        raise ValueError("variant names must be unique")
    if runs_per_cell < MIN_RUNS_PER_CELL:
        raise ValueError(
            f"runs_per_cell must be at least {MIN_RUNS_PER_CELL} "
            f"(METHODOLOGY.md section 3), got {runs_per_cell}"
        )

    rng = random.Random(seed)
    cells = [Cell(s, v) for s in scenarios for v in variants]
    runs: list[PlannedRun] = []
    position = 0

    if strategy is OrderStrategy.INTERLEAVED:
        for scenario in scenarios:
            for round_index in range(runs_per_cell):
                order = list(variants)
                rng.shuffle(order)
                for variant in order:
                    runs.append(
                        PlannedRun(
                            cell=Cell(scenario, variant),
                            replicate=round_index,
                            position=position,
                            round_index=round_index,
                        )
                    )
                    position += 1

    elif strategy is OrderStrategy.BLOCKED:
        for scenario in scenarios:
            for variant in variants:
                for replicate in range(runs_per_cell):
                    runs.append(
                        PlannedRun(
                            cell=Cell(scenario, variant),
                            replicate=replicate,
                            position=position,
                            round_index=replicate,
                        )
                    )
                    position += 1

    else:  # OrderStrategy.RANDOM
        for scenario in scenarios:
            pending = [
                (variant, replicate)
                for variant in variants
                for replicate in range(runs_per_cell)
            ]
            rng.shuffle(pending)
            for variant, replicate in pending:
                runs.append(
                    PlannedRun(
                        cell=Cell(scenario, variant),
                        replicate=replicate,
                        position=position,
                        round_index=replicate,
                    )
                )
                position += 1

    return RunPlan(
        runs=runs,
        strategy=strategy,
        seed=seed,
        runs_per_cell=runs_per_cell,
        cells=cells,
    )


def factorial_variants(factors: Sequence[str]) -> list[tuple[str, ...]]:
    """Every subset of ``factors``, for a full factorial interaction design.

    For ``["sodium", "lithium"]`` this yields the four cells the interaction
    term needs: ``()``, ``("sodium",)``, ``("lithium",)``, and both together.
    The empty tuple is the mod-free baseline and is always first.

    Capped at ``MAX_FACTORIAL_FACTORS`` because the design is 2^n and the run
    count is 2^n * runs_per_cell; five factors at seven runs is 224 full game
    launches. Larger sets should be screened pairwise instead.
    """
    if len(factors) > MAX_FACTORIAL_FACTORS:
        raise ValueError(
            f"full factorial is capped at {MAX_FACTORIAL_FACTORS} factors "
            f"(2^n growth); got {len(factors)}. Use pairwise screening instead."
        )
    if len(set(factors)) != len(factors):
        raise ValueError("factor names must be unique")

    return [
        combo
        for size in range(len(factors) + 1)
        for combo in itertools.combinations(factors, size)
    ]
