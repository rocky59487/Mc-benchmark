"""Run planning: execution order, repetition, and factorial designs.

Implements docs/METHODOLOGY.md sections 3 and 6.

The planner is small but it is where the fairness guarantee is actually made.
Everything else measures; this decides *when* each thing is measured, and that
ordering is what separates a real effect from thermal drift.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum

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
    """One (scenario, variant) pair: the unit that gets repeated."""

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


def _balanced_rounds(
    variants: Sequence[str], rounds: int, rng: random.Random
) -> list[list[str]]:
    """Variant orders for each round, balanced on position.

    Each round, the variant carrying the largest total of slot indices so far
    goes first and the smallest goes last. Ties are broken at random, so the
    schedule is not a fixed order an operator could arrange a result around.

    The effect is that consecutive rounds pair up into reversals of each other,
    which is the ABBA arrangement used to cancel drift in any experiment where
    the apparatus changes as it runs. A reversed pair contributes the same slot
    total to every variant, so the spread in mean slot is bounded, whatever the
    seed, by

        (len(variants) - 1) / rounds,  and exactly 0 when rounds is even

    because an even number of rounds leaves no round unpaired. Independent
    shuffling, which is what this did before, bounds it at nothing: over 400
    seeds of six variants and five rounds the worst spread was 3 slots.

    An even ``runs_per_cell`` is therefore worth preferring, and costs one
    extra run per cell over the odd number below it.

    A variant does run twice in a row across a reversal boundary, being last in
    one round and first in the next. That is inherent to ABBA and is the price
    of the cancellation. It is also cheap here: every run gets a fresh instance,
    a restored world and its own warmup, so what carries over is the machine's
    thermal state, which is the thing being balanced rather than a thing being
    measured.
    """
    totals = dict.fromkeys(variants, 0)
    schedule: list[list[str]] = []
    for _ in range(rounds):
        order = list(variants)
        rng.shuffle(order)
        order.sort(key=lambda variant: -totals[variant])
        for slot, variant in enumerate(order):
            totals[variant] += slot
        schedule.append(order)
    return schedule


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
    round, ordering the variants within each round so that no variant collects
    earlier slots than another across the suite:

        round 1:  B  A  C
        round 2:  C  A  B
        round 3:  A  C  B

    rather than the blocked ``AAAAA BBBBB CCCCC`` that every existing Minecraft
    benchmark uses. Blocked execution confounds variant with wall-clock time,
    and time carries thermal throttling, page-cache warming, background load,
    and ambient temperature drift. On a machine that throttles after ten
    minutes, blocked ordering hands a clean and repeatable win to whichever
    variant ran first, and it reproduces, which is what makes it hard to spot.

    Balanced rather than independently shuffled, which is what this did before
    and is not the same thing. Shuffling each round separately equalises mean
    position only in expectation, and a suite runs a handful of rounds, not
    enough for that to bite. Six variants over five rounds, on the seed the
    repository's own optimisation suite ships with, gave one variant a mean
    slot of 1.6 and another 3.6: two slots, every round, in the same direction.
    That is a constant offset between variants, so it survives averaging over
    replicates and the statistics price it as effect rather than noise. See
    :func:`_balanced_rounds` for what replaces it and what it guarantees.

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
            # Balanced per scenario, not once for the suite. Scenarios run
            # end to end, so each is its own stretch of wall-clock time and
            # needs its own balance; one shared schedule would leave the
            # second scenario carrying whatever imbalance the first ended on.
            schedule = _balanced_rounds(variants, runs_per_cell, rng)
            for round_index, order in enumerate(schedule):
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
