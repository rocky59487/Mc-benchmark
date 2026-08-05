# mcbench

**A controlled, reproducible performance benchmark for Minecraft mods, client and server.**

Most performance claims in the Minecraft ecosystem come from a single unrepeated
run, in blocked execution order, with no variance estimate. That procedure cannot
separate a real effect from thermal drift, and it will reproduce, because
whatever ran first keeps running first.

mcbench measures many mods in one pass under one methodology, and reports
`inconclusive` when the data does not support a verdict.

> **Status: the client chain has been run on real hardware; the server chain has
> not.** Sixty runs on an RTX 5070 Ti Laptop and a Ryzen 9 8940HX under Fabric
> 1.21.1 — six mod configurations across two client scenarios, five runs each,
> every one producing a measurement. Sodium came back at +63.2% average FPS
> [+59.9, +67.0]; EntityCulling and FerriteCore came back `inconclusive` on frame
> rate, which is what their own documentation predicts. The eight server scenarios
> are implemented and unit-tested but have never been run against a real server.
> See [Roadmap](#roadmap).

## What makes it different

| | Existing tools | mcbench |
|---|---|---|
| Repetition | Single run | ≥5 runs per cell, fresh process each time |
| Execution order | Blocked (`AAA BBB`) | **Interleaved, position-balanced** |
| Uncertainty | None | Bootstrap confidence intervals on everything |
| Verdicts | Point-estimate diffs | Effect size + region of practical equivalence |
| Multi-mod | Manual jar swapping | Declarative matrix, automated |
| Mod interactions | Ignored | Full factorial with an interaction term |
| Scope | Client **or** server | Client **and** server, one methodology |
| Says "I don't know" | Never | `inconclusive` is a first-class result |

Interleaving is the row that matters most. On a machine that throttles after ten
minutes, blocked ordering hands a clean and repeatable win to whatever ran first.
Balanced rather than shuffled, because shuffling each round independently only
equalises position in expectation and a suite runs a handful of rounds.

## What it measures

**Client** — frametime mean and percentiles, average FPS, 1% and 0.1% lows,
stutter rate, frametime coefficient of variation.

Aggregation happens in the time domain. Averaging per-second FPS computes a
harmonic mean of frametimes, which over-weights fast frames and hides stutter, so
every FPS figure is derived as `1000 / mean(frametime_ms)`. "1% low" means the
mean of the worst 1% of frames, not the 99th percentile; both definitions
circulate and they are not the same number.

**Server** — MSPT mean and percentiles, **tick headroom**, tick-warp throughput,
chunk generation and load rates.

TPS is demoted deliberately. Any server under budget reports exactly 20 TPS, so
TPS cannot distinguish 5 ms/tick from 30 ms/tick. Headroom (`1 - mspt/50`)
separates them, and TPS is reported only for saturated scenarios where it
becomes meaningful again.

**Both** — allocation rate, GC pause total and p99, steady-state and peak heap.

## Scenarios

Eleven scenarios ship today:

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

Villager AI is separated from general mob ticking because it stresses different
code, and optimisation mods frequently improve one while regressing the other.

Scenarios are **recipes, not world saves**: a seed plus a scripted setup
sequence, generated on your machine. Distributing a save is a licensing problem,
and it would also embed the generator version of whoever produced it.

## Quick start

```bash
pip install -e .

mcbench doctor                                     # can this machine measure?
mcbench scenarios                                  # list what's available
mcbench metrics                                    # the metric registry
mcbench validate --suite suites/example-performance-mods.toml
mcbench plan suites/example-performance-mods.toml  # inspect the schedule
mcbench resolve suites/example-performance-mods.toml --download

# Build the probe for your platform once. Every instance needs it, including the
# mod-free baseline: without it nothing in the game reads the scenario and no run
# produces a measurement stream.
(cd probe/adapters/probe-fabric && ../../gradlew build)

mcbench run suites/example-performance-mods.toml -o results.json \
    --probe-jar probe/adapters/probe-fabric/build/libs/mcbench-probe-fabric.jar
mcbench analyse results.json --export-dir report/  # charts + tables + HTML
```

On Windows use `gradlew.bat`; every Gradle project here ships both wrappers.
Building an adapter needs Gradle itself running on JDK 21, because Loom
decompiles Minecraft during configuration; a `JAVA_HOME` pointing at 17 fails
with "Minecraft 1.21.1 requires Java 21 but Gradle is using 17".

`--probe-jar` can be omitted when running from a checkout that has already built
it, since the harness looks in the usual build directory. It is never assumed: a
missing probe is a preflight blocker naming the build command, because a suite
that discovers it two hours in has produced nothing.

### Setting up HeadlessMC

mcbench launches the game through HeadlessMC and never handles credentials
itself. Once, before the first run:

```bash
java -jar headlessmc-launcher.jar --command login       # device code, in a browser
java -jar headlessmc-launcher.jar --command "download 1.21.1"
java -jar headlessmc-launcher.jar --command "fabric 1.21.1"
java -jar headlessmc-launcher.jar --command versions    # note the loader id
```

`login` prints a URL and a code; the process must stay running until the browser
step completes. `versions` lists the modded install under the loader's own id,
such as `fabric-loader-0.19.3-1.21.1`, and the `0.19.3` there is what belongs in
the suite's `loader_version`.

HeadlessMC keeps its account beside the directory it is run from, so point
`--headlessmc` at the jar and `doctor` will find the session:

```bash
mcbench doctor --headlessmc /path/to/headlessmc-launcher.jar
```

Every instance log opens with the exact command that produced it, the working
directory, and the probe environment, so a run that behaved oddly can be
reproduced by hand.

### Running server scenarios

HeadlessMC has no server command, so a server is launched directly from a jar
you already have. mcbench does not download or build one; a Fabric or Paper
installer produces a runnable jar, and that is what to name:

```bash
mcbench run suites/your-server-suite.toml -o results.json \
    --server-jar /path/to/fabric-server-launch.jar \
    --accept-eula
```

`--accept-eula` records that you accept the
[Minecraft EULA](https://aka.ms/MinecraftEULA). Server scenarios refuse to start
without it and mcbench will not accept it for you; the answer is written into
the results document as `eula_accepted`, because a published server result rests
on it. `accept_eula = true` in the suite manifest does the same thing.

A suite with no client scenarios needs no HeadlessMC at all.

### Headless use

`mcbench run` is meant to be driven by a developer in one command, or by CI with
no terminal at all:

```bash
mcbench run suite.toml --json-events -o results.json   # NDJSON progress for CI
mcbench doctor --json                                  # machine-readable gating
mcbench analyse results.json --format json             # structured verdicts
```

Benchmarking a build that is not published yet, the usual case during
development, uses a local jar:

```toml
[[variants]]
name = "my-dev-build"
mods = [{ platform = "local", project = "build/libs/mymod.jar", version = "1.2.0-dev" }]
```

It runs like any other variant and its file is hashed into the provenance, but
the suite is reported as **not publishable**: nobody else can obtain that jar.

### Preflight gating

`mcbench doctor` decides whether the machine can produce a number worth
believing, and `run` refuses to start if it cannot. It checks for a real GPU,
forced software rendering, a display, competing Minecraft processes, CPU
frequency scaling, battery, memory against the configured heap, disk,
virtualisation, a licensed account, HeadlessMC, and the probe artefact for the
selected platform.

Its full readings travel into the results bundle, not just a publishable verdict.
Two runs on machines differing in frequency scaling, virtualisation and free
memory would otherwise serialise identically.

The checks can block. Benchmarking a rendering mod on a machine with no GPU is
the easiest way to publish a meaningless Minecraft number: software rasterisation
does not just make things slower, it moves the work the mod exists to optimise
onto a different bottleneck.

Readings are taken per platform (`runner/hostinfo.py`), so the same gate applies
on Linux, Windows and macOS rather than degrading to "unknown" off Linux.

## Exporting charts and tables

`--export-dir` writes the full bundle:

```
report.html    self-contained: charts, sortable tables, no external requests
report.md      Markdown, for pasting into a PR or an issue
report.json    structured verdicts, for CI gates and the corpus
comparisons.csv / cells.csv / runs.csv     data tables (also tsv/md/html)
```

`runs.csv` carries every individual run before aggregation, so a reader can redo
the analysis instead of taking ours on trust.

The charts are hand-built SVG, with no plotting dependency, a colourblind-safe
palette, and light and dark themes:

- **Forest plot** — relative changes with intervals against a shaded ROPE band.
  It draws the verdict rule directly: an interval touching the band has not
  established anything.
- **Interval bars** — absolute values with confidence whiskers, zero-based.
- **Frametime CDF** — the whole distribution. Two variants can share a mean and
  differ sharply in the tail where stutter lives.
- **Order-effect scatter** — metric against execution position, to audit whether
  interleaving held on your machine.
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

Mods are named by coordinate. mcbench resolves and hash-verifies them at run time
on your machine; it never redistributes jars.

`mcbench validate` reports whether a suite is **publishable**: interleaved
ordering, at least 5 runs per cell, and every mod version pinned. A suite failing
any of these still runs, but its numbers are not comparable to anyone else's.

It also reports what a valid suite gives up. At exactly 5 runs per cell one run
lost to outlier rejection drops the cell below the floor and it reports no
verdict; an odd count leaves variants differing slightly in mean position. Both
cost one line in the manifest to avoid, and both are invisible until the hours
are spent.

That is not hypothetical. In the 60-run set above, three runs were disqualified
for world-fingerprint mismatch and one was lost to outlier rejection, which was
enough to leave the largest effect in the whole dataset — Sodium at +61% average
FPS, +179% 0.1% low — reporting `insufficient_data` instead of a verdict. The
advisory had said so before the suite started.

## Mod interactions

Two independently harmless mods can interact badly, competing for a lock,
invalidating each other's caches, or forcing a shared path off its fast route.
This is the usual explanation for a modpack that runs far worse than its parts
suggest, and no existing benchmark measures it.

Declare a factorial group and mcbench measures all four cells (`none`, `A`, `B`,
`A+B`) and reports the interaction term with its own confidence interval:

```
interaction = (cost(A+B) − cost(none)) − [(cost(A) − cost(none)) + (cost(B) − cost(none))]
```

Zero means the costs add. Positive means the pair is more expensive together than
their individual costs predict.

## Reading a verdict

Every comparison carries a relative change with a bootstrap CI, a Cliff's delta
effect size, and a verdict judged against a region of practical equivalence
(default ±2%):

- `improvement` / `regression` — the whole interval clears the ROPE
- `equivalent` — the whole interval sits inside the ROPE
- `inconclusive` — the interval straddles a boundary; more runs needed
- `insufficient_data` — fewer than five runs survived in an arm

A statistically detectable difference is not the same as one worth caring about.
With enough runs a 0.3% difference becomes detectable and is still irrelevant.

**Absolute numbers are comparable only within one session on one machine.**
Cross-machine comparison goes through ratios to `reference-hardware-baseline`,
run in the same session. Cross-machine absolute comparison is unsupported.

## The probe

`probe/` is the Java side: the component that runs inside Minecraft and samples
timing. It is split so that almost none of it depends on Minecraft.

`probe-core` has **zero Minecraft imports**. Phase control, steady-state
detection, sample buffering, the runtime monitor and protocol emission live
there, and its JUnit tests run in seconds with no game present.

A platform adapter implements three methods: a timing hook, a command executor,
and a shutdown request. That is the entire version-coupled surface, which is what
makes "every version, every platform" tractable — a new Minecraft version
normally needs no code change, only a rebuild.

Four adapters exist. Fabric, NeoForge and Forge are mod loaders with clients;
Paper is a server plugin platform with no client at all. Four unrelated
ecosystems, four nearly identical files, none containing methodology.

Underneath them sits a **JVM agent** that needs no loader. It times frames on any
version and any loader, including vanilla, by instrumenting
`org.lwjgl.glfw.GLFW.glfwSwapBuffers`: LWJGL is a third-party library, so its
names are never obfuscated and are byte-identical across every Minecraft version
and mapping scheme. It is a timing source rather than a platform, and cannot run
commands, which is the price of referencing no game class. See
[docs/PLATFORMS.md](docs/PLATFORMS.md).

The hot path is treated as hot: `recordFrame` allocates nothing, never blocks on
I/O, and hands off through a double-buffered `long[]`.

```bash
cd probe && ./gradlew test          # no Minecraft required
./gradlew jar
java -cp probe-core/build/libs/probe-core-0.1.0.jar \
    dev.mcbench.probe.core.SelfTest /tmp/out client 42
```

`SelfTest` drives a real `ProbeSession` with synthetic timings and writes a real
probe stream. The wire protocol has two independent implementations, the Java
writer and the Python reader, and a format with two implementations drifts unless
something checks. `tests/test_conformance.py` parses fixtures generated by it and
independently re-derives the warmup convergence the Java side claimed. Adapter
authors can validate against it without a GPU, an account, or a working instance.

## Diagnosing a modpack

Knowing a pack is slow is not actionable; knowing which of its ninety mods is
responsible is. Two tools, cheapest first.

### Static health check

```bash
mcbench inspect mods/            # or individual jars
mcbench inspect mods/ --json     # for CI
mcbench inspect mods/ --loader fabric --minecraft-version 1.21.1
```

Runs on jars alone: no game, no account, no GPU.

- **Declared incompatibilities, evaluated by version.** Authors already record
  what they break in Fabric's `breaks`, NeoForge's `incompatible` dependencies
  and Bukkit's `depend`, and it is machine-readable and routinely ignored. Ranges
  are evaluated rather than merely noted: `breaks lib <2` against an installed
  `lib 3` is not a conflict, and checking by presence alone reported it as one.
- **Structural problems.** Missing dependencies, duplicate mod ids, jars fighting
  over the same id, and dependencies satisfied at an incompatible version.
  `lib >=2` with `lib 1` present will not launch, and presence-only checking
  certified it as fine. Fabric API's module dependencies collapse into one
  finding, because six missing modules are one absent jar. Fabric API itself is
  not assumed present: it is an ordinary mod, and treating it as ambient hid the
  one dependency most Fabric packs actually miss.
- **Bundled jars.** Fabric libraries shipped inside their dependents count as
  present, so a complete pack stops being reported as missing them.
- **Mixin contention.** Which Minecraft classes more than one mod transforms,
  read from each mixin's declared configuration and its `@Mixin` annotation
  targets. Where no annotation can be read, the weaker constant-pool footprint is
  shown under its own heading rather than passed off as a declared target.
  Overlap is not proof of a conflict, but it is where conflicts come from, so it
  ranks where to look next.

Pass `--loader` and `--minecraft-version` to check the constraints that depend on
them; without a target those are reported as recorded-but-unverified rather than
silently passed. Anything the tool could not read is an error rather than a
warning, because a gate that exits zero on input it failed to parse is not a
gate.

Exit code is non-zero on blocking problems, so it drops into CI.

### Culprit isolation

```bash
mcbench bisect suite.toml --scenario visual-biome-flyby -o diagnosis.json
```

When a pack really is slow, this isolates the minimal responsible subset by delta
debugging. Four things make it more than a bisection.

**The oracle is statistical.** The same subset measured twice can disagree, so
probes return regression, clean, inconclusive or invalid, and only the first two
are evidence. If the full pack's regression cannot be confirmed, the search
refuses to start.

**Subsets are dependency-closed.** A culprit `bad` that needs `library` cannot be
tested alone: `{bad}` will not launch and `{library}` does not reproduce. Reading
both failures as "does not reproduce" concludes the two interact, and two mod
authors get a bug report about a conflict that does not exist. Every subset is
closed over its declared dependencies before launch, and the support mods that
pulls in are reported separately from the suspects.

**The culprit is often a pair.** Two mods can each be harmless and be
catastrophic together; a plain bisection splits them, sees neither half regress,
and concludes nothing is wrong. The complement phase of ddmin survives that. An
interaction is claimed only after minimality is checked by re-measuring the
candidate and testing every single removal, because ddmin's stopping condition
("no split narrowed anything") can also be reached by a run of unresolved probes.
A set that stopped there is reported as narrowed-but-unconfirmed.

**The baseline is re-measured for every probe.** Measuring it once would compare
an hour-three probe against an hour-zero machine state and attribute thermal
drift to a mod. Each probe interleaves baseline and subset in shuffled order,
which doubles the cost; `--reuse-baseline` takes the cheap path and marks the
result exploratory.

Each probe is a full benchmark cell, so the search is budgeted and reports what
it spent in probes and in game launches. Isolating one bad mod out of 64 takes
well under 40 probes. Every probe's numbers are kept in `diagnosis.json`.

### World fingerprinting

```bash
mcbench world work/instances/*/mcbench     # do these runs share a world?
```

Scenarios ship a seed and a setup script rather than a world save, so the world
is an output of a run rather than a fixed input. Two variants can therefore
measure different terrain, and averaging across that is not a comparison.

Every run is fingerprinted from its saved region files, and runs whose worlds
differ are flagged inadmissible, which makes
[METHODOLOGY.md](docs/METHODOLOGY.md) §7 enforceable. The hash covers block
palettes, packed block indices, biomes and chunk coordinates, and **excludes**
entities, tick lists and lighting: those differ between two runs of the same
variant, so including them would flag everything.

Reading the save needs no game API, so it works on every version and platform
including those with no adapter, and it runs after the process exits so it cannot
perturb what it qualifies.

## One scenario, every platform

Scenarios are platform-neutral. Compilation takes a target and the dialect layer
handles the differences: `/replaceitem` became `/item replace block` in 1.17,
`/tick warp` is vanilla from 1.20.3 but needs Carpet before that, and Paper has
no client at all.

```bash
mcbench targets --with-mod carpet          # which scenarios run where
mcbench targets --target paper:1.20.4 --json
```

A target that cannot express a scenario **refuses it with a reason** rather than
compiling something that half-executes. Adding a scenario means writing it once;
the matrix says where it runs and why not elsewhere. See
[docs/PLATFORMS.md](docs/PLATFORMS.md).

## Documentation

- **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)** — the specification. Start here.
- **[docs/RESEARCH.md](docs/RESEARCH.md)** — prior-art survey and the gap.
- **[docs/SECURITY.md](docs/SECURITY.md)** — threat model. mcbench downloads code
  and executes it; that is the function, not a flaw. Read before handling any
  external input.
- **[docs/PLATFORMS.md](docs/PLATFORMS.md)** — how one scenario runs on every
  platform and version.
- **[docs/LICENSING.md](docs/LICENSING.md)** — legal constraints that shaped the
  architecture. Read before adding a dependency or a data file.

## Roadmap

**Done**

- *Analysis* — bootstrap CIs, calibrated MAD outlier rejection, Cliff's delta,
  ROPE verdicts, interaction terms, FDR control; metric registry and per-run
  reduction; SVG charts, tables in four formats, self-contained HTML report.
- *Orchestration* — scenario schema, loader and 11 definitions; interleaved
  randomised planner; suite manifests with publishability checks; Modrinth and
  local-jar resolution with hash verification; preflight gating on Linux, Windows
  and macOS; the headless run loop.
- *Probe* — hot-path sample buffers, phase control with steady-state detection,
  JMX runtime monitoring, the wire protocol, and the adapter SPI. Adapters for
  Fabric, NeoForge, Forge and Paper, each built against a real toolchain, plus
  the LWJGL-instrumenting JVM agent that needs no loader at all.
- *Correctness* — the scenario-to-command compiler joining harness to probe;
  world fingerprinting, which makes METHODOLOGY §7 enforceable rather than
  asserted; a threat model and security hardening; CI enforcing tests, lint,
  protocol conformance, security regressions and licence compliance.

- *Validation* — sixty client runs on real hardware, which is what found the
  defects unit tests could not: a tick warp every server scenario declared and
  nothing issued, superflat layers six scenarios asked for and never got, and a
  fingerprint region drawn close enough to the edge of generation that it
  disqualified the largest effect in the dataset.

**Next** — the same run against a real server. Nothing has measured one yet;
`--server-jar` is what it now takes, since HeadlessMC cannot drive a server and
mcbench will not ship one. Then: CurseForge provider (opt-in, no caching);
cross-loader and cross-version comparison; bot-driven player load; the public
results corpus.

## Contributing

The methodology document is normative. A change to how a number is produced is a
change to `docs/METHODOLOGY.md` first and to the code second.

Before adding a dependency or data file, check the compliance checklist at the
end of [docs/LICENSING.md](docs/LICENSING.md). The short version: no game files,
no mod jars, no third-party world saves, and nothing GPL-licensed linked into the
harness.

## Licence

Apache-2.0, chosen over MIT for its explicit patent grant and over any copyleft
licence so that mod authors, hosts and platforms can adopt it freely. See
[NOTICE](NOTICE) for third-party attribution.
