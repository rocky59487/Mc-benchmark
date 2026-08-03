# mcbench

**A controlled, reproducible performance benchmark for Minecraft mods — client and server.**

Almost every performance claim in the Minecraft ecosystem comes from a single
unrepeated run, in blocked execution order, with no variance estimate. That
procedure cannot tell a real effect from thermal drift. It will report a
confident number, and the number will reproduce, and it can still be entirely an
artefact of which configuration happened to run first.

mcbench exists to make that distinction. It measures many mods in one pass,
under one methodology, and it says `inconclusive` when the data does not support
a verdict.

> **Status: the full chain is built; it needs a machine that can run Minecraft.**
> Methodology, statistics, scenarios, planner, mod resolution, headless run loop,
> preflight gating, chart/table export, the scenario-to-command compiler, the
> probe timing core, and adapters for Fabric and Paper are implemented and
> tested — 295 tests across Python and Java, including conformance fixtures in
> both directions of the harness/probe seam. What remains is more platforms and
> real hardware. See [Roadmap](#roadmap).

## What makes it different

| | Existing tools | mcbench |
|---|---|---|
| Repetition | Single run | ≥5 runs per cell, fresh process each time |
| Execution order | Blocked (`AAA BBB`) | **Interleaved, shuffled per round** |
| Uncertainty | None | Bootstrap confidence intervals on everything |
| Verdicts | Point-estimate diffs | Effect size + region of practical equivalence |
| Multi-mod | Manual jar swapping | Declarative matrix, automated |
| Mod interactions | Ignored | Full factorial with an interaction term |
| Scope | Client **or** server | Client **and** server, one methodology |
| Says "I don't know" | Never | `inconclusive` is a first-class result |

The interleaving row is the one that matters most. On a machine that throttles
after ten minutes, blocked ordering hands a clean, repeatable, *entirely fake*
win to whatever ran first — and because it reproduces, it looks like evidence.

## What it measures

**Client** — frametime mean and percentiles, average FPS, 1% and 0.1% lows,
stutter rate, frametime coefficient of variation.

All aggregation happens in the time domain. Averaging per-second FPS computes a
harmonic mean of frametimes, over-weighting fast frames and hiding exactly the
stutter players notice — so every FPS figure here is derived as
`1000 / mean(frametime_ms)`. "1% low" means the mean of the worst 1% of frames,
not the 99th percentile; both definitions circulate and they are not the same
number.

**Server** — MSPT mean and percentiles, **tick headroom**, tick-warp throughput,
chunk generation and load rates.

TPS is deliberately demoted. Any server under budget reports exactly 20 TPS, so
TPS cannot distinguish 5 ms/tick from 30 ms/tick — both score "perfect".
Headroom (`1 - mspt/50`) separates them, and TPS is reported only for
deliberately saturated scenarios where it becomes meaningful again.

**Both** — allocation rate, GC pause total and p99, steady-state and peak heap.

## Scenarios

Eleven scenarios ship today, covering every axis:

| Scenario | Side | Targets |
|---|---|---|
| `reference-hardware-baseline` | client | Cross-machine normalisation |
| `visual-biome-flyby` | client | Terrain meshing, chunk upload, culling |
| `visual-particle-storm` | client | Particle pipeline, frametime spikes |
| `entity-mobcap-saturation` | server | **Mob optimisation** — entity tick, AI, collision |
| `entity-villager-pathfinding` | server | **Mob optimisation** — brain system, pathfinder, POI |
| `worldgen-fresh-chunks` | server | **Generation optimisation** — noise, carvers, features |
| `redstone-observer-clocks` | server | Block updates, neighbour notification |
| `fluid-and-lighting-cascade` | server | Fluid scheduling + light engine |
| `block-entity-hopper-chains` | server | Block entity tick, inventory transfer |
| `chunkio-load-throughput` | server | Deserialisation, NBT, region I/O |
| `tick-stability-saturated` | server | **Tick stability** — degradation past budget |

Villager AI is separated from general mob ticking on purpose: it stresses
entirely different code, and optimisation mods frequently improve one while
regressing the other. A combined scenario would hide that.

Scenarios are **recipes, not world saves** — a seed plus a scripted setup
sequence, generated on your machine. That is a licensing requirement and also
the right call: a distributed save silently embeds the generator version of
whoever produced it.

## Quick start

```bash
pip install -e .

mcbench doctor                                     # can this machine measure?
mcbench scenarios                                  # list what's available
mcbench metrics                                    # the metric registry
mcbench validate --suite suites/example-performance-mods.toml
mcbench plan suites/example-performance-mods.toml  # inspect the schedule
mcbench resolve suites/example-performance-mods.toml --download
mcbench run suites/example-performance-mods.toml -o results.json
mcbench analyse results.json --export-dir report/  # charts + tables + HTML
```

No runtime dependencies. A measurement standard people are asked to trust should
be verifiable with a stock interpreter.

### Headless use

`mcbench run` is designed to be driven by a developer in one command or by CI
with no terminal at all:

```bash
mcbench run suite.toml --json-events -o results.json   # NDJSON progress for CI
mcbench doctor --json                                  # machine-readable gating
mcbench analyse results.json --format json             # structured verdicts
```

Benchmarking a build that is not published yet — the usual case during
development — uses a local jar:

```toml
[[variants]]
name = "my-dev-build"
mods = [{ platform = "local", project = "build/libs/mymod.jar", version = "1.2.0-dev" }]
```

It runs exactly like any other variant and its file is hashed into the
provenance, but the suite is reported as **not publishable**: nobody else can
obtain that jar, so the result is reproducible only on your machine.

### Preflight gating

`mcbench doctor` decides whether the machine can produce a number worth
believing, and `run` refuses to start if it cannot. It checks for a real GPU,
forced software rendering, a display, competing Minecraft processes, CPU
governor, battery, memory against the configured heap, disk, virtualisation, a
licensed account, and HeadlessMC.

The most important thing it does is **refuse**. Benchmarking a rendering mod on
a machine with no GPU is the easiest way to publish a meaningless Minecraft
number: software rasterisation does not just make things slower, it moves the
work the mod exists to optimise onto a completely different bottleneck. That is
a hard block, not a warning.

## Exporting charts and tables

`--export-dir` writes the full bundle:

```
report.html    self-contained: charts, sortable tables, no external requests
report.md      Markdown, for pasting into a PR or an issue
report.json    structured verdicts, for CI gates and the corpus
comparisons.csv / cells.csv / runs.csv     data tables (also tsv/md/html)
```

`runs.csv` carries every individual run before aggregation, so a reader can redo
the analysis instead of taking ours on trust. A benchmark that publishes only
summaries cannot be independently checked.

The charts are hand-built SVG — no plotting dependency, colourblind-safe
palette, light and dark themes:

- **Forest plot** — relative changes with intervals against a shaded ROPE band.
  The most important chart here, because it draws the verdict rule directly:
  an interval touching the band has not established anything.
- **Interval bars** — absolute values with confidence whiskers, zero-based.
- **Frametime CDF** — the whole distribution. Two variants can share a mean and
  differ sharply in the tail where stutter lives; a bar chart hides that.
- **Order-effect scatter** — metric against execution position, so you can audit
  whether interleaving actually held on your machine.
- **Interaction plot** — observed pair cost against the additive prediction.

## Defining a suite

```toml
name = "Client performance mods — 1.21.1 Fabric"
minecraft_version = "1.21.1"
loader = "fabric"
scenarios = ["visual-biome-flyby", "visual-particle-storm"]
baseline = "vanilla"
runs_per_cell = 7
order = "interleaved"
rope = 0.02

interactions = [["sodium", "entityculling", "sodium+entityculling"]]

[[variants]]
name = "vanilla"
mods = []

[[variants]]
name = "sodium"
mods = ["modrinth:sodium@mc1.21.1-0.6.0-fabric"]

[[variants]]
name = "sodium+entityculling"
mods = ["modrinth:sodium@mc1.21.1-0.6.0-fabric", "modrinth:entityculling@1.7.2"]
```

Mods are named by coordinate. mcbench resolves and hash-verifies them at run
time on your machine — it never redistributes jars.

Run `mcbench validate` and it will tell you whether a suite is **publishable**:
interleaved ordering, at least 5 runs per cell, and every mod version pinned. A
suite failing any of these still runs, but its numbers are not comparable to
anyone else's.

## Mod interactions

Two independently harmless mods can interact badly — competing for a lock,
invalidating each other's caches, forcing a shared path off its fast route. This
is the usual explanation for a modpack that runs far worse than its parts
suggest, and no existing benchmark measures it.

Declare a factorial group and mcbench measures all four cells (`none`, `A`, `B`,
`A+B`) and reports the interaction term with its own confidence interval:

```
interaction = (cost(A+B) − cost(none)) − [(cost(A) − cost(none)) + (cost(B) − cost(none))]
```

Zero means the costs add. Positive means the pair is more expensive together
than their individual costs predict.

## Reading a verdict

Every comparison carries a relative change with a bootstrap CI, a Cliff's delta
effect size, and one of four verdicts judged against a region of practical
equivalence (default ±2%):

- `improvement` / `regression` — the whole interval clears the ROPE
- `equivalent` — the whole interval sits inside the ROPE
- `inconclusive` — the interval straddles a boundary; more runs needed

A statistically detectable difference and a difference worth caring about are
not the same thing. With enough runs a 0.3% difference becomes detectable and is
still irrelevant, and a benchmark that reports it as a victory teaches people to
game it.

**Absolute numbers are comparable only within one session on one machine.**
Cross-machine comparison goes through ratios to `reference-hardware-baseline`,
run in the same session. Cross-machine absolute comparison is unsupported on
purpose — it cannot be done honestly.

## The probe

`probe/` is the Java side: the component that runs inside Minecraft and samples
timing. It is split so that almost none of it depends on Minecraft.

`probe-core` has **zero Minecraft imports**. Phase control, steady-state
detection, sample buffering, the runtime monitor, and protocol emission all live
there, and its 39 JUnit tests run in a second with no game present.

A platform adapter implements three methods — a timing hook, a command
executor, and a shutdown request. That is the entire version-coupled surface,
which is what makes "every version, every platform" tractable: a new Minecraft
version normally needs no code change, only a rebuild.

Two adapters exist, and they are the evidence the SPI is the right size. Fabric
(a mod loader with a client) and Paper (a server plugin platform with no client
at all) are unrelated ecosystems, yet their adapters are nearly the same length
and neither contains a single line of methodology. See
[docs/PLATFORMS.md](docs/PLATFORMS.md), including the JVM-agent approach that
can time frames on any version and any loader by instrumenting LWJGL — a
third-party library whose names are never obfuscated — instead of the game.

The hot path is treated as hot: `recordFrame` allocates nothing, never blocks on
I/O, and hands off through a double-buffered `long[]`. A probe that perturbs the
frame it is timing produces numbers about the probe.

```bash
cd probe && ./gradlew test          # 39 tests, no Minecraft required
./gradlew jar
java -cp probe-core/build/libs/probe-core-0.1.0.jar \
    dev.mcbench.probe.core.SelfTest /tmp/out client 42
```

`SelfTest` drives a real `ProbeSession` with synthetic timings and writes a real
probe stream. It exists because the wire protocol has two independent
implementations — the Java writer and the Python reader — and a format with two
implementations drifts unless something checks. `tests/test_conformance.py`
parses fixtures generated by it, and independently re-derives the warmup
convergence that the Java side claimed. Adapter authors can validate against it
without a GPU, an account, or a working instance.

## One scenario, every platform

Scenarios are platform-neutral. Compilation takes a target, and the dialect layer
handles the differences — `/replaceitem` became `/item replace block` in 1.17,
`/tick warp` is vanilla from 1.20.3 but needs Carpet before that, and Paper has
no client at all.

```bash
mcbench targets --with-mod carpet          # which scenarios run where
mcbench targets --target paper:1.20.4 --json
```

A target that cannot express a scenario **refuses it with a reason** rather than
compiling something that half-executes. Adding a scenario means writing it once;
the matrix tells you immediately where it runs and why not elsewhere. See
[docs/PLATFORMS.md](docs/PLATFORMS.md).

## Documentation

- **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)** — the specification. Start here.
- **[docs/RESEARCH.md](docs/RESEARCH.md)** — prior-art survey and the gap.
- **[docs/LICENSING.md](docs/LICENSING.md)** — legal constraints that shaped the
  architecture. Read before adding a dependency or a data file.

## Roadmap

**Done** — methodology spec; statistics engine (bootstrap CIs, calibrated MAD
outlier rejection, Cliff's delta, ROPE verdicts, interaction terms, FDR control);
metric registry and per-run reduction; scenario schema, loader, and 11
definitions; run planner with interleaved randomised ordering; suite manifests
with publishability checks; Modrinth and local-jar resolution with hash
verification; environment preflight gating; the headless run loop; the probe
wire protocol; SVG charts, table export in four formats, and the self-contained
HTML report. 182 tests.

Plus the probe's timing core: hot-path sample buffers, phase control with
steady-state detection, JMX runtime monitoring, the protocol writer, and the
adapter SPI — with cross-implementation conformance tests between the Java
writer and the Python reader.

Plus the scenario-to-command compiler that joins the harness to the probe, and
adapters for Fabric and Paper.

**Next:** NeoForge and Forge adapters, then the JVM agent that covers every
remaining version and loader.

**Then** — vsync detection in preflight (required before trusting agent-sourced
frame timings); world fingerprinting; CurseForge provider (opt-in, no caching);
cross-loader and cross-version comparison; bot-driven player load; the public
results corpus.

## Contributing

The methodology document is normative. A change to how a number is produced is a
change to `docs/METHODOLOGY.md` first, and to the code second.

Before adding a dependency or data file, check the compliance checklist at the
end of [docs/LICENSING.md](docs/LICENSING.md). The short version: no game files,
no mod jars, no third-party world saves, and nothing GPL-licensed linked into
the harness.

## Licence

Apache-2.0. Chosen over MIT for its explicit patent grant — a measurement
standard should not leave adopters exposed — and over any copyleft licence
because mod authors, hosts, and platforms need to be able to adopt it freely.
See [NOTICE](NOTICE) for third-party attribution.
