# Prior art survey

Conducted before any code was written, to establish whether mcbench needed to
exist. Summary: **it does.** Every piece of the problem has a point tool, and
nobody has assembled them into a controlled comparative benchmark.

## Client-side

### FPS Benchmark — the closest existing thing

<https://modrinth.com/project/Cfyw5WXC>

The most benchmark-shaped tool in the ecosystem, and the one whose scenario
taxonomy is worth learning from. It creates a temporary fixed-seed world, drives
a scripted camera sequence, and emits Markdown/JSON/CSV plus system metadata. 41
tests across 12 categories — showcase flyby, particles, entities, physics
(TNT/falling sand), redstone (observer clocks, piston arrays), lighting, fluids,
block entities (hoppers, comparators) — each with Quick/Full/Long presets. It can
diff two saved sessions into a `compare-A-vs-B.md`.

What it establishes: fixed seeds and scripted deterministic sequences are the
right primitives, and the category breakdown is broadly the right one.

Where it stops:

- **Fabric only, Minecraft 1.21–1.21.1 only, client only.**
- **One mod set per run.** Comparing mods means manually swapping jars and
  relaunching. There is no matrix, no orchestration, no automation.
- **No repetition or statistics.** A session is a single sample. There is no
  variance estimate, no confidence interval, and therefore no basis for saying a
  difference is real rather than noise.
- Session comparison is a two-way diff of point estimates, not an experiment.

### Performance Overlay

<https://modrinth.com/mod/performance-overlay>

Real-time FPS, frametime, 1% and 0.1% lows, stutter counts, worst spikes, GC and
memory. An excellent *instrument*; not a benchmark, because it supplies no
workload and no experimental control. Its metric set is a good target for
mcbench's own probe to match or exceed.

### PackBench

<https://github.com/zegevlier/PackBench>

Benchmarks resource packs, not mods. Different problem.

### BetterFps

<https://www.curseforge.com/minecraft/mc-mods/betterfps>

Historical. Its "benchmark" selects between sin/cos table implementations — a
micro-benchmark of one algorithm, not a system benchmark.

## Server-side

### spark — the standard profiler

<https://github.com/lucko/spark>

TPS, MSPT, a sampling profiler, and heap analysis, across Paper/Spigot/Fabric/
Forge/proxies. It is the correct tool for *diagnosing a server that is already
running*, and it is what everyone reaches for.

It is not a benchmark: it observes whatever load happens to exist. There is no
reproducible workload, no controlled comparison, no repetition. Two spark
reports from two servers are not comparable in any rigorous sense.

Note also that spark's core is GPLv3 — see [LICENSING.md](LICENSING.md).

### MCBenchmark / ServerBenchmark / Performance Checker

<https://modrinth.com/plugin/mcbenchmark> ·
<https://modrinth.com/plugin/serverbenchmark> ·
<https://modrinth.com/plugin/performance-checker>

Paper **plugins**. Two consequences: they cannot load or measure Fabric/Forge/
NeoForge mods at all, which excludes most of the modding ecosystem; and their
purpose is capacity diagnosis ("how many players will this box hold") rather
than comparative measurement between software configurations.

### Carpet's `/tick warp` — the key primitive

<https://github.com/gnembon/fabric-carpet>

Carpet removes the sleep between server ticks, running ticks as fast as the CPU
permits. This converts "how much work is N game-ticks of this scenario" into a
clean wall-clock measurement, unclamped by the 20 TPS ceiling.

This matters more than it first appears. Under normal ticking, any server
comfortably under budget reports exactly 20 TPS — so TPS **cannot distinguish**
a mod that uses 5 ms/tick from one that uses 30 ms/tick. Both look perfect. Tick
warp exposes the headroom that TPS hides.

Nobody has packaged this into a benchmark suite. mcbench does. Carpet is MIT, so
this is unencumbered.

## Automation and CI infrastructure

### mc-runtime-test / HeadlessMC

<https://github.com/headlesshq/mc-runtime-test> ·
<https://github.com/3arthqu4ke/headlessmc>

Runs a real Minecraft client inside CI via HeadlessMC plus Xvfb, covering 1.7.10
through current, on Forge, Fabric, and NeoForge. This is the right foundation
for headless automation and mcbench builds on it.

Its documentation is explicit that it verifies *only* that a mod loads and does
not crash. It measures no performance. Both are MIT.

### Fabric Loader JUnit and the GameTest framework

<https://docs.fabricmc.net//develop/automatic-testing>

Functional correctness testing. Useful patterns for driving in-game state
deterministically; no performance dimension.

### brucethemoose/Minecraft-Performance-Flags-Benchmarks

<https://github.com/brucethemoose/Minecraft-Performance-Flags-Benchmarks>

The only prior work found that automates *consecutive configurations with
averaged repeated runs, unattended* — which is the correct instinct. But it
benchmarks **JVM flags, not mods**, and its methodology document is marked
work-in-progress with the statistical treatment undocumented.

### packwiz

<https://github.com/packwiz/packwiz>

Git-friendly TOML modpack metadata. Not a benchmark, but exactly the right shape
for declaring mod sets under version control, and the direct inspiration for
mcbench's manifest format. MIT.

### SoulFire

<https://soulfiremc.com/blog/stress-testing-minecraft-servers>

Headless bot swarms for stress testing. The right tool for the player-load axis,
which scripted scenarios alone cannot cover.

## The state of published benchmark methodology

Worth recording, because it is the gap mcbench exists to close. Surveying how
mod and host performance comparisons are actually published, the common practice
is to hold hardware, version, and workload roughly constant while **not**
controlling the world seed, and to present single-run point estimates. At least
one published comparison is candid that its numbers should be read as practical
workload comparisons rather than laboratory results.

No public methodology found in the Minecraft space reports repetition counts,
variance, confidence intervals, or effect sizes. Comparisons are routinely
declared from differences well inside plausible run-to-run noise.

## Conclusion — the gap

The primitives exist and are nearly all permissively licensed. What does not
exist anywhere:

1. **Multi-mod matrix orchestration.** Automatic provision → run → tear down →
   next variant, without a human swapping jars.
2. **Statistical rigour.** Repetition, warmup discard, outlier handling,
   confidence intervals, effect sizes, and a defensible rule for when a
   difference counts as real.
3. **Order and drift control.** Interleaved randomised execution so that thermal
   throttling and background load cannot masquerade as a mod's effect.
4. **Client and server measured by one harness** under one methodology.
5. **Cross-loader and cross-version comparison** — the same mod on Fabric versus
   NeoForge, or across game versions.
6. **Interaction effects.** Measuring A, B, and A+B to detect when mods are
   non-additive. Universally ignored today, and the single most common source of
   surprising real-world performance.
7. **Hardware normalisation**, so results from different contributors compose
   into a shared corpus instead of being mutually incomparable.

Items 2, 3, and 6 are where mcbench's claim to authority rests. Items 1, 4, and
5 are engineering. Item 7 is what turns the project into a standard rather than
a tool.
