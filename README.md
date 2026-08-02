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

> **Status: foundation complete, execution backend in progress.**
> The methodology, statistics engine, scenario suite, planner, mod resolution,
> and reporting are implemented and tested. The in-game probe and instance
> orchestration are the next milestone — see [Roadmap](#roadmap).

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

mcbench scenarios                                  # list what's available
mcbench metrics                                    # the metric registry
mcbench validate --suite suites/example-performance-mods.toml
mcbench plan suites/example-performance-mods.toml  # inspect the schedule
mcbench resolve suites/example-performance-mods.toml --download
mcbench analyse results.json                       # aggregate into a report
```

No runtime dependencies. A measurement standard people are asked to trust should
be verifiable with a stock interpreter.

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
with publishability checks; Modrinth resolution with hash verification;
Markdown/JSON reporting. 130 tests.

**Next** — the in-game probe mod (Fabric/NeoForge) emitting frametime and tick
streams; instance orchestration via HeadlessMC + Xvfb; world fingerprinting;
environment quiescence checks; CurseForge provider (opt-in, no caching).

**Later** — cross-loader and cross-version comparison; bot-driven player load;
the public results corpus.

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
