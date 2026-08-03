# Measurement methodology

This is the specification mcbench's implementation must satisfy. It is the
project's actual product: anyone can time a game loop, but a number is only
authoritative if the procedure that produced it is defensible and reproducible.

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
| `frametime_cv` | Coefficient of variation — smoothness, independent of speed |

The "1% low" is defined as the **mean of the worst 1% of frames**, not the 99th
percentile. Both definitions circulate; they are not the same number, and the
mean-of-worst form is the one that responds to how bad the bad frames are. The
definition is stated in every report so results are never ambiguous.

`frametime_cv` deserves emphasis. It is the answer to "is this smooth", asked
separately from "is this fast". A mod that raises average FPS while increasing
frametime variance has made the game feel worse, and every FPS-only benchmark in
the ecosystem will score it as an improvement.

### Reported server metrics

| Metric | Definition |
|---|---|
| `mspt_mean` | Mean milliseconds per tick |
| `mspt_p95/p99` | Tail tick cost |
| `tick_headroom` | `1 - mspt_mean / 50` — fraction of the budget left unused |
| `warp_throughput` | Ticks per wall-clock second under tick warp |
| `tps_effective` | Only meaningful when the server is saturated |

**TPS is deliberately demoted.** Any server under budget reports exactly 20 TPS,
so TPS cannot distinguish a 5 ms/tick configuration from a 30 ms/tick one — both
score "perfect". Headroom and MSPT are the informative quantities. TPS is
reported only for saturated scenarios, where it becomes meaningful again.

### Cross-cutting metrics

Allocation rate (bytes/sec), GC pause total and p99, peak and steady-state heap,
chunk generation and load throughput, and worker-thread CPU time. Allocation
rate matters independently of pause time: a mod that doubles allocation without
raising measured pauses has moved the cost onto whoever runs a smaller heap.

---

## 2. Warmup and steady state

The JVM is the single largest confound in Minecraft benchmarking. The first
seconds of any run measure the interpreter and the JIT compiler, not the mod.

Every run has three phases:

1. **Provision** — untimed. World generation, chunk load, mod init.
2. **Warmup** — timed but discarded. Default 60 s client, 2000 ticks server.
3. **Measurement** — retained.

Warmup ends when **both** conditions hold:

- the configured minimum duration has elapsed, **and**
- steady state is detected: over a trailing window, the JIT compilation count
  has plateaued and the rolling median frametime is stable within a tolerance
  (default 5%).

If steady state is not reached by a hard ceiling (default 3× the minimum), the
run is retained but **flagged `warmup_not_converged`**, and any result built
from it carries that flag through to the report. Silently accepting an
unconverged run is how a slow-starting mod gets credited with the JIT's warmup
cost.

Never discard warmup by frame *count*. A slow configuration reaches a given
frame count later in wall-clock time and therefore gets a longer real warmup —
which biases the comparison toward the slow configuration.

---

## 3. Repetition and execution order

### Repetition

A single run is not a measurement. Minimum **5 independent runs per cell**
(scenario × variant), default 7, where each run is a full fresh process launch.
Repeats within one process share JIT state, page cache, and heap history, and
systematically understate variance.

### Interleaving is mandatory

The default execution order is **round-robin across variants, with the variant
order shuffled each round**:

```
round 1:  B  A  C          <- order shuffled
round 2:  A  C  B
round 3:  C  B  A
...
```

Not blocked (`AAAAA BBBBB CCCCC`).

This is the single most important control in the document, and no existing
Minecraft benchmark does it. Blocked execution confounds variant with time, and
time carries thermal throttling, background load, page-cache warming, and
ambient temperature drift. A machine that throttles after ten minutes will hand
a clean, repeatable, entirely fake win to whatever ran first. Interleaving
converts that systematic bias into noise the statistics can see and account for.

Seeds for the shuffle are recorded so the exact order is reproducible.

### Environment quiescence

Before a suite, mcbench records and reports: CPU governor and frequency limits,
core count and affinity, available memory, disk queue depth, thermal state where
readable, and running process count. It **refuses to run** by default if another
Minecraft process is detected. It **warns** on laptop battery power, on active
CPU frequency scaling, and on detected virtualization with unstable timing.

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
  correction to the MAD scale. This targets contaminated runs — an OS update, a
  stray process — not slow frames.
- **Every exclusion is reported**, with its value and its distance in MAD. A
  cell that loses more than 20% of its runs is flagged as unstable rather than
  quietly averaged.

### Why the threshold is 8 and not 3

8 MAD is far more permissive than the ~3 conventionally used for outlier
screening. The choice is calibrated, not arbitrary.

At benchmark sample sizes (5–10 runs) the centre and the scale are estimated
from the same handful of points, which makes the studentised deviation
heavy-tailed. Measured against clean Gaussian samples, a 3.5 threshold falsely
flags roughly **13%** of seven-run cells; at 8 that falls to about **1.6%**,
while gross contamination — the kind an OS update or a stray process produces,
typically tens of MAD out — is still caught essentially every time.

The two errors are not symmetric, and that asymmetry decides it. Falsely
excluding a run removes a tail observation, narrows the confidence interval, and
makes the benchmark **more confident than the data warrants**. Failing to
exclude a mildly unusual run merely widens the interval and pushes the verdict
toward `inconclusive`. One of those failure modes publishes a wrong answer; the
other publishes an honest "we don't know". The threshold is set accordingly.

A consequence worth stating plainly: at 5–7 runs, distinguishing a genuinely
contaminated run from ordinary variance is **statistically underpowered**, and
no threshold fixes that. Cells needing that discrimination need more runs, not
a more aggressive filter.

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

### The ROPE rule — the verdict

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
| `inconclusive` | CI straddles a ROPE boundary — needs more runs |

`inconclusive` is a first-class outcome and is reported as prominently as the
others. A benchmark that never says "we don't know" is not measuring, it is
guessing. mcbench reports how many additional runs would be needed to resolve an
inconclusive cell.

### Multiple comparisons

A suite comparing many mods across many scenarios runs many tests, and some will
look significant by chance. Where more than one variant is compared against a
common baseline, mcbench applies Benjamini–Hochberg false-discovery-rate control
across the family and reports both raw and adjusted verdicts.

---

## 6. Interaction effects

Mod performance is not additive, and the assumption that it is causes most
real-world surprises. Two independently harmless mods can interact
catastrophically — competing for the same lock, invalidating each other's
caches, forcing a shared code path off its fast route.

For a declared interaction set `{A, B}`, mcbench measures four cells — `none`,
`A`, `B`, `A+B` — and reports the **interaction term**:

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

This is a [licensing requirement](LICENSING.md) and independently the right
engineering call: a distributed world save silently embeds the generator version
of whoever made it, and mods that alter worldgen would be measured against a
world their own generator never produced.

Because worldgen output itself varies between game versions and between
worldgen-altering mods, mcbench records a **world fingerprint** with every run.
Runs whose fingerprints differ are never pooled. Where a mod under test
legitimately changes worldgen, that is surfaced as a finding, not averaged away.

The fingerprint is a hash over **block content only**: the block state palette,
the packed block indices, and biomes, taken from the saved region files after the
run. Chunk coordinates are included, so the same terrain generated in a different
place is correctly a different world.

Deliberately **excluded**: entities, block-entity contents, tick lists, lighting,
structure references, and inhabited time. Every one of those differs between two
runs of the *same* variant — random ticks fire differently, mobs spawn in
different places, lighting is recomputed. Hashing them would flag every run as a
mismatch, and a check that always fires is a check nobody reads.

Two consequences follow from reading the save rather than asking the game:

- It needs no game API, so it works identically on every version and platform,
  including those with no probe adapter, and it runs after the process exits so
  it cannot perturb the measurement it qualifies.
- A world that **could not be read** produces no fingerprint at all rather than a
  placeholder. Two runs that both failed to compute one must never be pooled on
  the strength of agreeing about nothing.

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
Cross-machine *absolute* comparison remains unsupported, on purpose — it cannot
be done honestly, and claiming otherwise would be the fastest way to lose the
credibility this project depends on.

---

## 9. What mcbench will not claim

Stated explicitly, because a standard is defined as much by its limits:

- It does not produce a single scalar "mod performance score". Performance is
  multidimensional and collapsing it discards the information that matters.
- It does not compare absolute numbers across machines.
- It does not measure subjective feel, correctness, gameplay, or compatibility.
- It does not extrapolate from a benchmark scenario to arbitrary real gameplay.
  A scenario is a controlled proxy, and reports name which proxy was used.
- It does not report a result it cannot reproduce. `inconclusive` is preferred
  to a confident guess, always.
