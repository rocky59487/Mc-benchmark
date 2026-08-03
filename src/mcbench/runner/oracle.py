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
from ..stats import DEFAULT_ROPE, MIN_ADMISSIBLE_RUNS, Verdict, compare

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
    baseline_attempts: int = 0
    subset_attempts: int = 0
    """Launches made for each arm, successful or not. Distinct from the length
    of the value lists, which counts only the ones that produced a number."""
    min_runs: int = MIN_ADMISSIBLE_RUNS

    @property
    def attempts(self) -> int:
        return self.baseline_attempts + self.subset_attempts

    @property
    def usable(self) -> bool:
        """Whether this probe retained enough runs to support a verdict.

        The floor applies per arm. Checking only that both lists are non-empty
        is what let a probe with four failures out of five in each arm report a
        regression of ``+100% [+100%, +100%]`` from one value against one — the
        most decisive-looking output the system can produce, from an experiment
        that almost entirely failed.
        """
        return (
            len(self.baseline_values) >= self.min_runs
            and len(self.subset_values) >= self.min_runs
        )

    @property
    def unlaunchable(self) -> bool:
        """True when the subset never produced a measurement and the baseline did.

        Same machine, same probe, interleaved order: if every launch of the
        subset failed while the baseline's succeeded, the difference is the mod
        set, not the machine. That is a broken configuration rather than a
        statistical dead end, and the two must not both read as "no regression".
        """
        return bool(self.baseline_values) and not self.subset_values


class BenchmarkOracle:
    """Answers whether a mod subset reproduces a regression.

    Args:
        measure: Runs one replicate and returns the metric, or None if the run
            failed. Failed runs are dropped rather than substituted, since
            inventing a value would be fabricating a measurement.
        metric: Which metric decides. Its polarity comes from the registry, so a
            throughput metric is not mistaken for a cost metric.
        runs_per_probe: Replicates of *each* arm. The floor is
            :data:`~mcbench.stats.MIN_ADMISSIBLE_RUNS` from METHODOLOGY section
            3, and it is enforced rather than merely documented — the previous
            minimum of 2 accepted a probe that could not produce an admissible
            verdict under the project's own rules, so every probe it ran was
            wasted before it started.
        interleave_baseline: Keep True for anything you intend to act on.
        rope: Region of practical equivalence. A subset must be worse by more
            than this before it is called a culprit — otherwise the search would
            chase differences too small to matter and never converge.
        allow_underpowered: Escape hatch for tests and dry runs. Probes issued
            under it can only ever return ``INCONCLUSIVE`` for want of runs, so
            it buys speed and no answers.
    """

    def __init__(
        self,
        measure: MeasureCell,
        *,
        metric: str = "frametime_mean_ms",
        runs_per_probe: int = MIN_ADMISSIBLE_RUNS,
        rope: float = DEFAULT_ROPE,
        seed: int = 0,
        interleave_baseline: bool = True,
        baseline_mods: Sequence[str] = (),
        min_runs: int = MIN_ADMISSIBLE_RUNS,
        allow_underpowered: bool = False,
    ) -> None:
        if metric not in METRICS:
            raise ValueError(
                f"unknown metric {metric!r}; see mcbench.metrics.METRICS"
            )
        if runs_per_probe < min_runs and not allow_underpowered:
            raise ValueError(
                f"runs_per_probe must be at least {min_runs} for a probe to be "
                f"able to reach a verdict, got {runs_per_probe}. Every arm needs "
                f"that many *surviving* runs, so a probe planned at fewer cannot "
                f"succeed even when nothing fails. Pass allow_underpowered=True "
                f"to run anyway and receive inconclusive verdicts."
            )
        if runs_per_probe < 1:
            raise ValueError(f"runs_per_probe must be positive, got {runs_per_probe}")

        self.measure = measure
        self.metric = metric
        self.definition = METRICS[metric]
        self.runs_per_probe = runs_per_probe
        self.rope = rope
        self.seed = seed
        self.min_runs = min_runs
        self.interleave_baseline = interleave_baseline
        self.baseline_mods = tuple(baseline_mods)
        self.records: list[ProbeRecord] = []
        self._cached_baseline: list[float] | None = None
        self._cached_baseline_attempts = 0
        #: Every call into ``measure``, successful or not. This is the number
        #: that bounds a diagnosis in wall-clock terms, because a failed launch
        #: costs the same minutes as a successful one.
        self.measurement_calls = 0

    # -- measurement -----------------------------------------------------

    def _run_arm(self, mods: Sequence[str], replicate: int) -> float | None:
        self.measurement_calls += 1
        return self.measure(list(mods), replicate)

    def _paired_measure(self, subset: Sequence[str]) -> ProbeRecord:
        """Measure baseline and subset, interleaved.

        One replicate of each arm per round, with the order shuffled, so neither
        arm systematically occupies the earlier — cooler, quieter — half of the
        probe.
        """
        rng = random.Random(self.seed + len(self.records) * 7919)
        record = ProbeRecord(subset=tuple(subset), min_runs=self.min_runs)

        for replicate in range(self.runs_per_probe):
            arms = [("baseline", self.baseline_mods), ("subset", tuple(subset))]
            rng.shuffle(arms)
            for name, mods in arms:
                value = self._run_arm(mods, replicate)
                if name == "baseline":
                    record.baseline_attempts += 1
                else:
                    record.subset_attempts += 1
                if value is None:
                    # A failed run is dropped, never substituted. Filling it in
                    # would be inventing a measurement — but the attempt is still
                    # counted, because it cost a launch and it is evidence about
                    # whether this configuration runs at all.
                    continue
                if name == "baseline":
                    record.baseline_values.append(value)
                else:
                    record.subset_values.append(value)

        return record

    def _sequential_measure(self, subset: Sequence[str]) -> ProbeRecord:
        """Cheap path: measure the baseline once and reuse it.

        Only for exploratory use. Everything a long search attributes to a mod
        this way may equally be drift between when the baseline ran and when the
        probe did.
        """
        record = ProbeRecord(subset=tuple(subset), min_runs=self.min_runs)

        if self._cached_baseline is None:
            values = []
            attempts = 0
            for replicate in range(self.runs_per_probe):
                attempts += 1
                value = self._run_arm(self.baseline_mods, replicate)
                if value is not None:
                    values.append(value)
            self._cached_baseline = values
            self._cached_baseline_attempts = attempts

        record.baseline_values = list(self._cached_baseline)
        record.baseline_attempts = self._cached_baseline_attempts

        for replicate in range(self.runs_per_probe):
            record.subset_attempts += 1
            value = self._run_arm(subset, replicate)
            if value is not None:
                record.subset_values.append(value)
        return record

    # -- verdict ---------------------------------------------------------

    def __call__(self, subset: Sequence[str]) -> Outcome:
        if self.interleave_baseline:
            record = self._paired_measure(subset)
        else:
            record = self._sequential_measure(subset)

        baseline_values = record.baseline_values
        subset_values = record.subset_values

        if record.unlaunchable:
            # Every launch of this subset failed while the baseline launched in
            # the same interleaved probe on the same machine. That is a property
            # of the mod set, and calling it "no regression" would exonerate a
            # configuration that never ran.
            record.outcome = Outcome.INVALID
            record.note = (
                f"the subset never produced a measurement in "
                f"{record.subset_attempts} attempt(s) while the baseline "
                f"produced {len(baseline_values)}; treating it as an "
                f"unlaunchable configuration, not as a clean result"
            )
            self.records.append(record)
            return record.outcome

        if not record.usable:
            # Not enough surviving runs to say anything. Reporting "clean" would
            # quietly exonerate a mod on the strength of a failed measurement.
            record.outcome = Outcome.INCONCLUSIVE
            record.note = (
                f"insufficient successful runs: baseline "
                f"{len(baseline_values)}/{record.baseline_attempts}, subset "
                f"{len(subset_values)}/{record.subset_attempts}; "
                f"{self.min_runs} are required in each arm"
            )
            self.records.append(record)
            return record.outcome

        result = compare(
            baseline_values,
            subset_values,
            lower_is_better=self.definition.lower_is_better,
            rope=self.rope,
            seed=self.seed + len(self.records),
            min_runs=self.min_runs,
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

        ``insufficient_data`` becomes INCONCLUSIVE rather than CLEAN, for the
        same reason the whole four-state model exists: the runs to answer were
        never obtained, which is not an answer of "no".
        """
        if verdict is Verdict.REGRESSION:
            return Outcome.REGRESSION
        if verdict in (Verdict.INCONCLUSIVE, Verdict.INSUFFICIENT_DATA):
            return Outcome.INCONCLUSIVE
        return Outcome.CLEAN

    # -- reporting -------------------------------------------------------

    @property
    def total_runs(self) -> int:
        """Game launches spent, the number that actually bounds a diagnosis.

        Counts every measurement invocation, including the ones that failed. A
        failed launch costs the same minutes as a successful one, so a cost
        figure that omitted them would understate the price of a diagnosis
        exactly when the diagnosis was going badly — ten launches with eight
        failures previously reported as two.
        """
        return self.measurement_calls

    @property
    def successful_runs(self) -> int:
        """Launches that produced a usable metric value."""
        return sum(
            len(r.baseline_values) + len(r.subset_values) for r in self.records
        )

    @property
    def failed_runs(self) -> int:
        return self.total_runs - self.successful_runs

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
                "baseline_attempts": r.baseline_attempts,
                "subset_attempts": r.subset_attempts,
                "min_runs": r.min_runs,
                "note": r.note,
            }
            for r in self.records
        ]
