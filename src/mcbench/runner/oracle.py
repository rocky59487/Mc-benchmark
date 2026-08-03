"""The statistical oracle that culprit isolation consults.

:mod:`mcbench.diagnose` knows how to search a mod set; this is what answers
"does this subset reproduce the regression". Each answer costs a full benchmark
cell, and the answer is a verdict under the project's usual rules — bootstrap
interval, region of practical equivalence, and ``inconclusive`` when the data
does not support either call.

**Why the baseline is re-measured for every probe.** The obvious design measures
the mod-free baseline once and compares every later subset against it. That is
precisely the blocked execution order that ``docs/METHODOLOGY.md`` section 3
rejects: a bisection over ninety mods runs for hours, and a subset probed at hour
three would be compared against a machine state from hour zero. Thermal drift,
background load and page-cache warming would all be attributed to the mods.

So a probe interleaves: baseline and subset alternate, replicate by replicate,
with the order shuffled each round. It doubles the cost of every probe, and it is
the difference between a culprit and an artefact of when the probe happened to
run. The cheap alternative is available behind an explicit flag, and results
obtained that way are marked so they cannot be mistaken for the real thing.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..diagnose import Outcome
from ..metrics import METRICS
from ..stats import DEFAULT_ROPE, Verdict, compare

__all__ = [
    "MeasureCell",
    "ProbeRecord",
    "BenchmarkOracle",
]

#: Runs one replicate with the given mod set and returns the metric value.
#: Injected so the oracle can be tested without launching a game, and so the
#: same logic serves the harness, a dry run, or a replay of recorded data.
MeasureCell = Callable[[Sequence[str], int], float | None]


@dataclass
class ProbeRecord:
    """What one probe measured, kept so a diagnosis can be audited."""

    subset: tuple[str, ...]
    baseline_values: list[float] = field(default_factory=list)
    subset_values: list[float] = field(default_factory=list)
    outcome: Outcome = Outcome.INCONCLUSIVE
    relative_delta: float = 0.0
    ci: tuple[float, float] = (0.0, 0.0)
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.baseline_values) and bool(self.subset_values)


class BenchmarkOracle:
    """Answers whether a mod subset reproduces a regression.

    Args:
        measure: Runs one replicate and returns the metric, or None if the run
            failed. Failed runs are dropped rather than substituted, since
            inventing a value would be fabricating a measurement.
        metric: Which metric decides. Its polarity comes from the registry, so a
            throughput metric is not mistaken for a cost metric.
        runs_per_probe: Replicates of *each* arm. The default of 5 is the floor
            from METHODOLOGY section 3; below it there is no variance estimate.
        interleave_baseline: Keep True for anything you intend to act on.
        rope: Region of practical equivalence. A subset must be worse by more
            than this before it is called a culprit — otherwise the search would
            chase differences too small to matter and never converge.
    """

    def __init__(
        self,
        measure: MeasureCell,
        *,
        metric: str = "frametime_mean_ms",
        runs_per_probe: int = 5,
        rope: float = DEFAULT_ROPE,
        seed: int = 0,
        interleave_baseline: bool = True,
        baseline_mods: Sequence[str] = (),
    ) -> None:
        if metric not in METRICS:
            raise ValueError(
                f"unknown metric {metric!r}; see mcbench.metrics.METRICS"
            )
        if runs_per_probe < 2:
            raise ValueError(
                f"runs_per_probe must be at least 2 to estimate variance, "
                f"got {runs_per_probe}"
            )

        self.measure = measure
        self.metric = metric
        self.definition = METRICS[metric]
        self.runs_per_probe = runs_per_probe
        self.rope = rope
        self.seed = seed
        self.interleave_baseline = interleave_baseline
        self.baseline_mods = tuple(baseline_mods)
        self.records: list[ProbeRecord] = []
        self._cached_baseline: list[float] | None = None

    # -- measurement -----------------------------------------------------

    def _run_arm(self, mods: Sequence[str], replicate: int) -> float | None:
        return self.measure(list(mods), replicate)

    def _paired_measure(
        self, subset: Sequence[str]
    ) -> tuple[list[float], list[float]]:
        """Measure baseline and subset, interleaved.

        One replicate of each arm per round, with the order shuffled, so neither
        arm systematically occupies the earlier — cooler, quieter — half of the
        probe.
        """
        rng = random.Random(self.seed + len(self.records) * 7919)
        baseline_values: list[float] = []
        subset_values: list[float] = []

        for replicate in range(self.runs_per_probe):
            arms = [("baseline", self.baseline_mods), ("subset", tuple(subset))]
            rng.shuffle(arms)
            for name, mods in arms:
                value = self._run_arm(mods, replicate)
                if value is None:
                    # A failed run is dropped, never substituted. Filling it in
                    # would be inventing a measurement.
                    continue
                (baseline_values if name == "baseline" else subset_values).append(value)

        return baseline_values, subset_values

    def _sequential_measure(
        self, subset: Sequence[str]
    ) -> tuple[list[float], list[float]]:
        """Cheap path: measure the baseline once and reuse it.

        Only for exploratory use. Everything a long search attributes to a mod
        this way may equally be drift between when the baseline ran and when the
        probe did.
        """
        if self._cached_baseline is None:
            values = []
            for replicate in range(self.runs_per_probe):
                value = self._run_arm(self.baseline_mods, replicate)
                if value is not None:
                    values.append(value)
            self._cached_baseline = values

        subset_values = []
        for replicate in range(self.runs_per_probe):
            value = self._run_arm(subset, replicate)
            if value is not None:
                subset_values.append(value)
        return list(self._cached_baseline), subset_values

    # -- verdict ---------------------------------------------------------

    def __call__(self, subset: Sequence[str]) -> Outcome:
        if self.interleave_baseline:
            baseline_values, subset_values = self._paired_measure(subset)
        else:
            baseline_values, subset_values = self._sequential_measure(subset)

        record = ProbeRecord(
            subset=tuple(subset),
            baseline_values=baseline_values,
            subset_values=subset_values,
        )

        if not record.usable:
            # Not enough surviving runs to say anything. Reporting "clean" would
            # quietly exonerate a mod on the strength of a failed measurement.
            record.outcome = Outcome.INCONCLUSIVE
            record.note = (
                f"insufficient successful runs "
                f"(baseline {len(baseline_values)}, subset {len(subset_values)})"
            )
            self.records.append(record)
            return record.outcome

        result = compare(
            baseline_values,
            subset_values,
            lower_is_better=self.definition.lower_is_better,
            rope=self.rope,
            seed=self.seed + len(self.records),
        )
        record.relative_delta = result.relative_delta.value
        record.ci = (result.relative_delta.ci.low, result.relative_delta.ci.high)
        record.outcome = self._outcome_for(result.verdict)
        record.note = (
            f"{result.relative_delta.value * 100:+.2f}% "
            f"[{result.relative_delta.ci.low * 100:+.2f}%, "
            f"{result.relative_delta.ci.high * 100:+.2f}%] → {result.verdict.value}"
        )
        self.records.append(record)
        return record.outcome

    @staticmethod
    def _outcome_for(verdict: Verdict) -> Outcome:
        """Map a ROPE verdict onto what the search needs to know.

        ``equivalent`` becomes CLEAN because the search asks "does this subset
        reproduce the regression", and a difference inside the ROPE does not —
        it is a real but irrelevant difference. Collapsing it into REGRESSION
        would make the search chase noise-sized effects forever.

        ``improvement`` is also CLEAN: a subset that is *faster* plainly is not
        the culprit being hunted.
        """
        if verdict is Verdict.REGRESSION:
            return Outcome.REGRESSION
        if verdict is Verdict.INCONCLUSIVE:
            return Outcome.INCONCLUSIVE
        return Outcome.CLEAN

    # -- reporting -------------------------------------------------------

    @property
    def total_runs(self) -> int:
        """Game launches spent, the number that actually bounds a diagnosis."""
        return sum(
            len(r.baseline_values) + len(r.subset_values) for r in self.records
        )

    def audit(self) -> list[dict]:
        """Every probe with its numbers, so a diagnosis can be checked."""
        return [
            {
                "subset": list(r.subset),
                "outcome": r.outcome.value,
                "relative_delta": r.relative_delta,
                "ci": list(r.ci),
                "baseline_runs": len(r.baseline_values),
                "subset_runs": len(r.subset_values),
                "note": r.note,
            }
            for r in self.records
        ]
