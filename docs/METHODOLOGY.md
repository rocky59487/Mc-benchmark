# Measurement methodology

This is the specification mcbench's implementation must satisfy.

Every rule below exists because a specific, known effect will otherwise produce
a confident wrong answer.

---

## 1. What we measure, and why not FPS

### Frametime is the measurement; FPS is a presentation

mcbench records **per-frame durations in nanoseconds**, never a frame counter.
Aggregating FPS directly is a well-known error: FPS is the reciprocal of
frametime, so an arithmetic mean of per-second FPS samples is a harmonic mean of
frametimes, which systematically over-weights fast frames and hides exactly the
stutter that players notice.

Rule: **aggregate in the time domain, convert to FPS only for display.** A
reported "average FPS" is always `1 / mean(frametime)`.

### Reported client metrics

| Metric | Definition |
|---|---|
| `frametime_mean_ms` | Arithmetic mean of retained frame durations |
| `fps_avg` | `1000 / frametime_mean_ms` |
| `frametime_p50/p95/p99_ms` | Percentiles of the frametime distribution |
| `fps_1pct_low` | `1000 / mean(worst 1% of frametimes)` |
| `fps_0p1pct_low` | `1000 / mean(worst 0.1% of frametimes)` |
| `stutter_rate` | Frames exceeding 2× the running median, per 1000 frames |
| `frametime_cv` | Coefficient of variation: smoothness, independent of speed |

The "1% low" is defined as the **mean of the worst 1% of frames**, not the 99th
percentile. Both definitions circulate; they are not the same number, and the
mean-of-worst form is the one that responds to how bad the bad frames are. The
definition is stated in every report so results are never ambiguous.

`frametime_cv` answers "is this smooth" separately from "is this fast". A mod
that raises average FPS while increasing frametime variance has made the game
feel worse, and every FPS-only benchmark in the ecosystem scores it as an
improvement.

### Reported server metrics

| Metric | Definition |
|---|---|
| `mspt_mean` | Mean milliseconds per tick |
| `mspt_p95/p99` | Tail tick cost |
| `tick_headroom` | `1 - mspt_mean / 50`, the fraction of budget left unused |
| `warp_throughput` | Ticks per wall-clock second under tick warp |
| `tps_effective` | Only meaningful when the server is saturated |

**TPS is deliberately demoted.** Any server under budget reports exactly 20 TPS,
so TPS cannot distinguish a 5 ms/tick configuration from a 30 ms/tick one; both
score "perfect". Headroom and MSPT are the informative quantities. TPS is
reported only for saturated scenarios, where it becomes meaningful again.

**MSPT means the tick's execution time**, bracketed from the start of the tick
to its end, or read from a platform API that measures the tick itself. The
interval between consecutive end-of-tick callbacks is not MSPT: the server loop
sleeps out the remainder of the budget, so on an unsaturated server that interval
sits at 50 ms whether the tick cost 5 ms or 30 ms, which is the exact distinction
headroom exists to make.

A platform that can expose neither a bracket nor a duration publishes
`tick_period_mean_ms`, `tick_period_p95_ms` and `tick_period_p99_ms` instead,
and the run is flagged `tick_period_only`. Those figures are informative once a
server is over budget, where the period does track tick cost; below it they
describe the scheduler. They are never reported as MSPT and never compared
against it.

### Cross-cutting metrics

GC pause total, p99 and maximum, collection count, peak and steady-state heap,
chunk generation and load throughput.

**Pause percentiles come from individual collections.** They are read from the
JVM's GC notifications, one event per collection, each with its own duration and
its own heap readings before and after, so `heap_steady_mb` is the live set
measured at the collection rather than at whatever point the next sampling
interval fell. Where a JVM cannot supply per-event data, the total pause time is
reported and the percentile is omitted: a percentile over per-interval sums
describes the sampling cadence, not the collector.

**Allocation is either measured or named differently.** `alloc_rate_mb_s` comes
from the JVM's allocation counter and is reported only where that counter exists.
Where it does not, the figure published is `heap_growth_rate_mb_s`, a floor on
allocation rather than a measure of it: anything allocated and collected between
two samples never appears, and on a busy tick that is most of it. Allocation
matters independently of pause time, because a mod that doubles allocation
without raising measured pauses has moved the cost onto whoever runs a smaller
heap or a different collector.

---

## 2. Warmup and steady state

The JVM is the single largest confound in Minecraft benchmarking. The first
seconds of any run measure the interpreter and the JIT compiler, not the mod.

Every run has four phases:

1. **Provision**, untimed. World load, mod init.
2. **Setup**, untimed. The scenario's setup commands build the world it
   describes. It is a phase of its own rather than part of warmup: when setup
   shared the warmup budget, a scenario whose setup ran long entered measurement
   with commands still changing the world, and two machines gave the same
   scenario different effective warmup purely because setup took longer on one.
3. **Warmup**, timed but discarded. Default 60 s client, 2000 ticks server.
   Begins only once every setup command has succeeded and the world has settled
   for one second, with every warmup window and counter reset at that moment.
   Setup's own very uniform samples would otherwise satisfy the plateau test
   immediately.
4. **Measurement**, retained.

Warmup ends when **all three** conditions hold:

- the configured minimum duration has elapsed, **and**
- the rolling median frametime or tick time is stable within a tolerance
  (default 5%) across consecutive windows, **and**
- JIT compilation has plateaued: `CompilationMXBean.getTotalCompilationTime()`
  grows by less than 10 ms across three consecutive observations.

The compilation condition is checked, not assumed. A timing series can look flat
while tiered compilation is still promoting hot methods, and measurement that
starts there charges the mod for the compiler's remaining work. On a JVM that
cannot report compilation time the condition is skipped and the run records that
it was unavailable, rather than reporting a gate that never ran.

If the gate is not satisfied by a hard ceiling (default 3× the minimum), the run
is retained but **flagged `warmup_not_converged`**, and any result built from it
carries that flag through to the report. The run also records *which* condition
failed, because "did not converge" alone does not tell an operator whether to
lengthen the ceiling or to find a quieter machine. Silently accepting an
unconverged run is how a slow-starting mod gets credited with the JIT's warmup
cost.

Never discard warmup by frame count. A slow configuration reaches a given frame
count later in wall-clock time and therefore gets a longer real warmup, biasing
the comparison toward the slow configuration.

---

## 3. Repetition and execution order

### Repetition

A single run is not a measurement. Minimum **5 independent runs per cell**
(scenario × variant), default 7, where each run is a full fresh process launch.
Repeats within one process share JIT state, page cache, and heap history, and
systematically understate variance.

### Interleaving is mandatory

The default execution order is **round-robin across variants, balanced so that
no variant collects earlier positions than another**:

```
round 1:  B  A  C
round 2:  C  A  B          <- reverses round 1
round 3:  A  C  B
...
```

Not blocked (`AAAAA BBBBB CCCCC`).

This is the most important control in the document, and no existing Minecraft
benchmark does it. Blocked execution confounds variant with time, and time
carries thermal throttling, background load, page-cache warming and ambient
temperature drift. A machine that throttles after ten minutes hands a clean and
repeatable win to whatever ran first.

**Balanced, not merely shuffled.** Shuffling each round independently equalises
mean position only in expectation, and a suite runs a handful of rounds. Six
variants over five rounds can leave one variant averaging two positions earlier
than another in every round — a constant offset that survives averaging over
replicates and is read as effect rather than noise. Each round therefore orders
variants by accumulated position, latest-so-far first, ties broken at random.
Consecutive rounds pair into reversals, the ABBA arrangement used wherever the
apparatus drifts as it runs, and the spread in mean position is bounded for any
seed by `(variants - 1) / rounds`, exactly zero when the round count is even.

**Prefer an even `runs_per_cell`.** It balances exactly, and it leaves the
margin the next section requires: at the 5-run minimum, one run lost to outlier
rejection takes a cell below the floor and it reports no verdict at all.

Seeds are recorded so the exact order is reproducible.

### Environment quiescence

Before a suite, mcbench records and reports: CPU governor and frequency limits,
core count and affinity, available memory, disk queue depth, thermal state where
readable, and running process count. It **refuses to run** by default if another
Minecraft process is detected. It **warns** on laptop battery power, on active
CPU frequency scaling, and on detected virtualization with unstable timing.

That check happens once and a suite runs for hours, so **every run also looks
either side of its own launch** and carries `environment_noisy` if it found a
Minecraft process that was not ours. Either side rather than during, because our
own game is one and could not be told apart from anyone else's; a competitor
confined to a single run is therefore missed, and the flag's absence is not a
certificate.

Recorded rather than refused. Whether a second process mattered depends on what
it was doing, which mcbench cannot know, and discarding runs on suspicion throws
away hours over a launcher left open at its menu.

These are reported in every result. A result whose environment was not quiescent
is not silently discarded, but it is marked, and the public corpus filters on it.

---

## 4. Outlier handling

Frametime and MSPT distributions are heavily right-skewed with genuine long
tails. Two errors are common here:

- Removing outliers with a **standard-deviation** rule. The outliers inflate the
  standard deviation, so the rule fails exactly when it is needed.
- Removing outliers at all, at the wrong level.

mcbench's rule:

- **Within a run, nothing is removed.** Long frames are the phenomenon under
  study. A GC pause is a real cost and must reach the tail metrics.
- **Across runs, robust detection only.** A whole run may be excluded if its
  summary statistic is more than **8 MAD** (median absolute deviation) from the
  median of runs in the same cell, using a Croux–Rousseeuw small-sample
  correction to the MAD scale. This targets contaminated runs, such as an OS
  update or a stray process, rather than slow frames.
- **Every exclusion is reported**, with its value and its distance in MAD. A
  cell that loses more than 20% of its runs is flagged as unstable rather than
  quietly averaged.

### Why the threshold is 8 and not 3

8 MAD is far more permissive than the ~3 conventionally used for outlier
screening. The choice is calibrated, not arbitrary.

At benchmark sample sizes (5–10 runs) the centre and the scale are estimated
from the same handful of points, which makes the studentised deviation
heavy-tailed. Measured against clean Gaussian samples, a 3.5 threshold falsely
flags roughly **13%** of seven-run cells; at 8 that falls to about **1.6%**.
Gross contamination, the kind an OS update or a stray process produces, is
typically tens of MAD out and is still caught essentially every time.

The two errors are not symmetric, which is what decides the threshold. Falsely
excluding a run removes a tail observation, narrows the confidence interval and
makes the benchmark **more confident than the data warrants**. Failing to exclude
a mildly unusual run widens the interval and pushes the verdict toward
`inconclusive`.

At 5–7 runs, distinguishing a genuinely contaminated run from ordinary variance
is **statistically underpowered**, and no threshold fixes that. Cells needing
that discrimination need more runs rather than a more aggressive filter.

---

## 5. Uncertainty and comparison

### Bootstrap confidence intervals, not standard error

Frametime distributions are not normal, run counts are small, and the quantities
of interest (percentiles, 1% lows) have no closed-form standard error. mcbench
uses **percentile bootstrap** confidence intervals (default 10 000 resamples,
95%), which assume nothing about distribution shape and work for any statistic.

Bootstrap RNG is seeded and recorded, so intervals are reproducible.

### Effect size before significance

For a comparison between a baseline and a variant, mcbench reports:

- **Relative delta** with a bootstrap CI on the delta itself.
- **[Cliff's delta](https://en.wikipedia.org/wiki/Effect_size)**, a
  non-parametric ordinal effect size: the probability that a random run from one
  group beats a random run from the other. Robust to skew, meaningful for small
  samples, and it answers the question people actually have.

### The ROPE rule, and the verdict

A statistically detectable difference and a difference worth caring about are
different things. With enough runs, a 0.3% difference becomes detectable and is
still irrelevant.

mcbench defines a **region of practical equivalence (ROPE)**, default **±2%**,
and issues one of four verdicts:

| Verdict | Condition |
|---|---|
| `improvement` | Entire delta CI is beyond the ROPE, in the better direction |
| `regression` | Entire delta CI is beyond the ROPE, in the worse direction |
| `equivalent` | Entire delta CI lies inside the ROPE |
| `inconclusive` | CI straddles a ROPE boundary; needs more runs |
| `insufficient_data` | Fewer than 5 runs survived in an arm; no verdict at all |

`inconclusive` is a first-class outcome and is reported as prominently as the
others. mcbench also reports how many additional runs would resolve it.

`insufficient_data` is distinct, and the distinction is load-bearing.
Inconclusive means the runs were made and disagreed; insufficient means the runs
required to answer were never obtained. Conflating them lets a mostly-failed
experiment read as a measured null. In the worst case one surviving value against
one surviving value is a difference of 100% with a zero-width interval, the most
decisive-looking output the system can produce from the least evidence it holds.

The floor is therefore enforced wherever a verdict is issued rather than merely
documented: five surviving values per arm for that metric, after inadmissible
runs and outlier rejection. Below it the direction is withheld, the interval is
not printed, and the report says how many runs are missing. The same floor
governs interaction terms, a difference of differences being the least robust
quantity in the report, and the bisect oracle, which declines to convict on a
probe that mostly failed to launch.

### Multiple comparisons

A suite comparing many mods across many scenarios runs many tests, and some will
look significant by chance. Where more than one variant is compared against a
common baseline, mcbench applies Benjamini–Hochberg false-discovery-rate control
across the family and reports both raw and adjusted verdicts.

A family is one **(scenario, metric)**: the variants sharing a baseline cell and
a metric, among which a chance extreme would be picked out and reported. Metrics
are not pooled into a single suite-wide family: `frametime_mean_ms` and `fps_avg`
are the same measurement twice, and correcting across strongly dependent tests is
punitive rather than principled.

Correction can only remove discoveries. A decisive verdict that does not survive
is reported as `inconclusive`; a verdict the ROPE rule already declined to make
is never promoted by it. Every comparison carries its raw p-value, its adjusted
q-value, its family, and both the raw and the corrected verdict, so a reader can
tell whether the correction ran at all.

---

## 6. Interaction effects

Mod performance is not additive, and the assumption that it is causes most
real-world surprises. Two independently harmless mods can interact
catastrophically by competing for the same lock, invalidating each other's
caches, or forcing a shared code path off its fast route.

For a declared interaction set `{A, B}`, mcbench measures four cells (`none`,
`A`, `B`, `A+B`) and reports the **interaction term**:

```
interaction = (cost(A+B) - cost(none)) - [(cost(A) - cost(none)) + (cost(B) - cost(none))]
```

Zero means the effects add. Positive means the pair costs more together than
apart. The term carries its own bootstrap CI and ROPE verdict.

This scales as 2^n, so full factorial designs are capped (default n ≤ 4) and
larger sets fall back to screening the baseline plus each pair.

---

## 7. Determinism and provenance

### Worlds are generated from recipes, never distributed

A scenario ships a seed, a generator configuration, a spawn point, and a scripted
setup sequence. The world is generated on the operator's machine.

This is a [licensing requirement](LICENSING.md), and independently correct: a
distributed world save embeds the generator version of whoever made it, and mods
that alter worldgen would be measured against a world their own generator never
produced.

Because worldgen output itself varies between game versions and between
worldgen-altering mods, mcbench records a **world fingerprint** with every run.
Runs whose fingerprints differ are never pooled. Where a mod under test
legitimately changes worldgen, that is surfaced as a finding, not averaged away.

The fingerprint is a hash over **block content only**: the block state palette,
the packed block indices, and biomes, taken from the saved region files after the
run. Chunk coordinates are included, so the same terrain generated in a different
place is correctly a different world.

Deliberately **excluded**: entities, block-entity contents, tick lists, lighting,
structure references and inhabited time. Every one of those differs between two
runs of the same variant, because random ticks fire differently, mobs spawn in
different places and lighting is recomputed. Hashing them would flag every run as
a mismatch.

Two consequences follow from reading the save rather than asking the game:

- It needs no game API, so it works identically on every version and platform,
  including those with no probe adapter, and it runs after the process exits so
  it cannot perturb the measurement it qualifies.
- A world that **could not be read** produces no fingerprint at all rather than a
  placeholder. Two runs that both failed to compute one must never be pooled on
  the strength of agreeing about nothing.
- A world with **no chunks** is the same case wearing a hash. Nothing failed to
  read, so the world is complete, and the digest of nothing is a constant every
  empty world shares. It is refused for the same reason.

The reference world for a scenario is the fingerprint the **majority** of its
runs share, not the first observed. Run order must not decide which world counts
as correct, or one bad early run would condemn every good one after it.

### Everything that could change a number is recorded

Every result carries: exact game version, loader and loader version, full
resolved mod list with versions and file hashes, JVM vendor/version/flags/heap,
OS and kernel, CPU model and core count, GPU and driver version, display
resolution and refresh rate, all game settings, scenario version and hash, the
mcbench version, and every RNG seed used.

**A result without complete provenance is not admitted to the public corpus.**

**Recorded means observed, not requested.** Most of that list is what the suite
asked for, and a launcher may satisfy a request with something else: its own
JVM, a different loader build, a scenario file edited since the plan was made.
The probe reports what the game actually was — platform, scenario and its
content hash, Minecraft version, loader version, JVM, and the framebuffer it
rendered into — and every run's record carries both that and the disagreements
with what was asked.

A disagreement about the experiment (scenario, its hash, Minecraft, loader) is
`configuration_mismatch` and the run is inadmissible: the numbers are real and
describe something the document does not. A disagreement about the JVM or the
window is recorded and pooled, because every variant shared that launch, so it
makes the description wrong rather than the comparison invalid.

### Scenarios are versioned

Scenario definitions are content-hashed and semantically versioned. A change to a
scenario that could move its numbers is a major version bump, and results across
a major bump are never pooled or compared. Silent scenario drift would destroy
the value of a longitudinal corpus.

---

## 8. Hardware normalisation

Contributors run different hardware, so raw numbers do not compose. mcbench's
approach:

- Every suite runs a fixed, mod-free **reference scenario** on vanilla, in the
  same session as the mods under test.
- Results are reported **primarily as ratios to that reference**, measured on
  the same machine in the same session.
- Absolute numbers are always shown alongside, never instead.

A ratio to a same-session baseline cancels most hardware and environment
differences, which is what makes cross-contributor aggregation legitimate.
Cross-machine absolute comparison remains unsupported.

---

## 9. What mcbench will not claim

- It does not produce a single scalar "mod performance score". Performance is
  multidimensional and collapsing it discards the information that matters.
- It does not compare absolute numbers across machines.
- It does not measure subjective feel, correctness, gameplay or compatibility.
- It does not extrapolate from a benchmark scenario to arbitrary real gameplay.
  A scenario is a controlled proxy, and reports name which proxy was used.
- It does not report a result it cannot reproduce. `inconclusive` is preferred to
  a confident guess.
