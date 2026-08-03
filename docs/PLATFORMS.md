# Platform and version support

The requirement is every Minecraft version and every mod platform. This document
is how that is made tractable rather than a maintenance disaster.

## The core problem

Supporting many Minecraft versions is not hard because the code is hard. It is
hard because **game APIs move**, and every game API an adapter touches is a
place where a new version can break it. Obfuscated names change every release,
mapping schemes differ between loaders, and packages get reorganised.

So the strategy is not "write more adapters". It is **touch as little of
Minecraft as possible**, and push everything else into code that has no idea
Minecraft exists.

## The architecture

```
probe-core         zero Minecraft imports. All methodology lives here.
   ↑
ProbeAdapter       the SPI: 3 methods an adapter must implement
   ↑
adapters           Fabric · NeoForge · Forge · Paper · JVM agent
```

`probe-core` contains phase control, steady-state detection, sample buffering,
the runtime monitor, and protocol emission — every rule in
[METHODOLOGY.md](METHODOLOGY.md). It compiles and is unit-tested with no game
present at all, which is why its tests run in CI in under a second.

An adapter supplies exactly three things:

1. a timing hook — `onFrame()` for frames, and for ticks one of
   `onTickStart()`/`onTickEnd()`, `recordPlatformTick(long)`, or
   `onTickPeriod()`,
2. `executeCommand(String)`, returning whether the game accepted the command,
3. `requestShutdown()`.

That is the entire version-coupled surface. A new Minecraft version usually
needs **no code change** — only a rebuild against that version's mappings.

### Ticks are bracketed; frames are not

A frame's duration *is* the interval between successive buffer swaps: the
renderer presents as fast as it can, so there is no idle to include. A server
tick is the opposite. The loop sleeps out whatever remains of the 50 ms budget,
so the interval between end-of-tick callbacks converges on 50 ms whether the
tick cost 5 ms or 30 ms — which is why timing ticks that way turned `mspt_mean`,
the percentiles and `tick_headroom` into measurements of the scheduler.

So an adapter reports ticks by whichever of these its platform supports, in
order of preference:

| Method | When | Published as |
|---|---|---|
| `recordPlatformTick(long)` | The platform measures ticks itself (Paper's `getTickTimes()`) | `mspt_*` |
| `onTickStart()` / `onTickEnd()` | The platform has pre- and post-tick events (Fabric, Forge, NeoForge) | `mspt_*` |
| `onTickPeriod()` | Neither is available | `tick_period_*`, run flagged |

The third is a real option, not a failure — but its samples are never published
as MSPT. An honest measurement of the tick period beats a mislabelled
measurement of the scheduler.

A platform that keeps its durations in a ring buffer needs `TickTimeRing`, not
an index computed from a tick number. Paper writes the ring at
`tickCount % length` but `Bukkit.getCurrentTick()` reports a *different*
counter, derived from wall-clock time, so indexing by it reads an arbitrary
slot — which still holds a real duration from some recent tick, and so produces
plausible numbers instead of an error. `TickTimeRing` diffs consecutive
snapshots and takes whatever changed, which needs no knowledge of the indexing
and recovers on its own after a gap.

### Commands report their outcome

`executeCommand` returns a boolean because a command can be refused without
throwing anything at all: a syntax error, an unknown selector, a missing
permission. Dispatch through the platform's command *dispatcher* rather than a
convenience wrapper — `executeWithPrefix` and `performPrefixedCommand` both log
the failure and return normally, so a scenario built on a mistyped `/fill` would
run to completion having built none of the world it describes.

A rejected setup command makes the run inadmissible. That is deliberate: the
numbers it produces are real, they look entirely ordinary, and they describe a
different experiment.

## Why the surface is this small

Three decisions do most of the work:

**Frame timing is measured between hook calls, not by wrapping the frame.** The
interval between consecutive swap-buffer calls is the same quantity as a
bracketed frame timer, but needs one hook point instead of two, and does not
require injecting into rendering internals.

**Workload is driven by commands, not by API calls.** Commands are the most
stable interface Minecraft has — `/summon`, `/fill`, `/setblock`, and
`/gamerule` have been compatible for a decade, while the Java methods behind
them have been renamed repeatedly. A scenario is a list of command strings, so
scenarios are portable across versions for free.

**Scenario interpretation stays in the harness.** The probe receives a flat
`.properties` file and a plain command list, not the scenario JSON. It needs no
parser, no dependency, and no knowledge of the methodology it is enforcing.

## Per-platform status

| Platform | Timing hook | Status |
|---|---|---|
| Fabric | `HudRenderCallback` / `ServerTickEvents` | **builds against 1.21.1** |
| Paper / Spigot / Purpur | repeating `Scheduler` task | **builds against paper-api** |
| NeoForge | `ClientTickEvent` / `ServerTickEvent` | **builds against 1.21.1** |
| Forge | `TickEvent.RenderTickEvent` / `TickEvent.ServerTickEvent` | **builds against 1.21.1** |
| Quilt | Fabric adapter loads directly | expected to work as-is |
| **Any version, any loader** | **JVM agent**, `glfwSwapBuffers` | **builds, runs, frames only** — see below |

Forge is the one loader with a per-frame event of its own —
`TickEvent.RenderTickEvent` fires once per frame with no mixin required. Fabric
has to reach for `HudRenderCallback` and NeoForge has nothing at all, which is
part of why the agent exists. All four adapters target 1.21.1 deliberately: a
cross-loader comparison is only meaningful when the game underneath is the same
build, so any difference measured is the loader.

## The universal fallback: a JVM agent

Loader adapters cover the modern ecosystem, but they inherit each loader's
version range, and none reaches back to the versions people still run.

There is a way to time frames on **any Minecraft version and any loader,
including vanilla, with zero Minecraft coupling**:

Since 1.13, Minecraft renders through LWJGL 3 and calls
`org.lwjgl.glfw.GLFW.glfwSwapBuffers` exactly once per presented frame. LWJGL is
a third-party library, so **its class and method names are never obfuscated** —
they are identical across every Minecraft version, every loader, and every
mapping scheme. A `-javaagent` that instruments that single call gets exact
frame boundaries without referencing one game class. For 1.12.2 and earlier the
LWJGL 2 equivalent is `org.lwjgl.opengl.Display.update()`.

Server ticks have no comparable third-party anchor, so servers keep using
per-loader adapters — but those are the smaller problem, since server-side APIs
have been far more stable than rendering internals.

This is built, in `probe/adapters/probe-agent`. It is verified against the
**real** LWJGL artefact pulled from Maven, not against a stand-in shaped the way
we imagined LWJGL to be, and an integration test forks a JVM with `-javaagent`
and asserts a parseable stream comes out.

```bash
cd probe/adapters/probe-agent && ./gradlew shadowJar   # no Minecraft needed
# then add to any launch, on any version, any loader, vanilla included:
#   -javaagent:/path/to/mcbench-probe-agent-0.1.0.jar
```

It is inert unless `MCBENCH_PROBE_CONFIG` is set, so it is safe to leave on a
launch command permanently — which is the only way anyone actually keeps it
there.

### What the agent is not

It is a **timing source, not a platform**. It supplies frame timings and nothing
else: it cannot execute commands, because it has no access to any game API —
that is exactly the price of not coupling to one. So it either runs alongside a
loader adapter that drives the workload, or measures a workload driven some
other way. An agent-only run of a scenario that needs setup commands would
produce clean frame timings for a world that was never built, which is worse
than no measurement.

It therefore writes to `probe-agent.jsonl`, deliberately separate from an
adapter's `probe.jsonl`. Both can be active at once, and two writers appending
to one file would interleave into an unparseable mess.

The harness combines them by **substitution, never merging**
(`adopt_agent_frames`). When an adapter already produced frames, both streams
timed the *same* frames by different means; concatenating them would double the
sample count and shrink every confidence interval to match — manufacturing
precision that was never measured. So the adapter wins, and the agent's frames
are used only when there are none to compete with. Adoption is also refused
when the two streams disagree about which scenario they measured: instance
directories are rebuilt per run so that should be impossible, but a stale file
contributing frames from a different variant would wreck a comparison while
looking perfectly healthy.

### Two failure modes it is built around

**Its own classes must not fork across classloaders.** The obvious way to make
the hook visible everywhere is `appendToBootstrapClassLoaderSearch(agentJar)`.
It does make it visible — and it also puts a *second* copy of the agent's own
classes on another loader, whose halves cannot see each other's package-private
state. The first version did this and died on launch with an
`IllegalAccessError`, having passed every unit test. The bootstrap append is
gone; `AgentIntegrationTest` keeps it gone.

**It must refuse rather than half-work.** The injected instruction names
`FrameHook` literally, so if the loader that owns LWJGL cannot resolve it, the
call becomes a `NoClassDefFoundError` *inside the render loop* — a crash on the
first frame. The transformer checks reachability per classloader (and that the
class found is the same one the agent initialised, not a second never-started
copy) and declines to instrument when the answer is no, printing why. Losing
frame timing is recoverable; breaking the game we were asked to measure is not.

Remaining tradeoffs, stated up front: it needs a bytecode library (ASM,
relocated to `dev.mcbench.probe.agent.shadow.asm` because Mixin ships its own
copy on every modded launch), and it measures *presented* frames, which on a
machine with vsync enabled measures the display rather than the renderer. The
harness must therefore verify vsync is off before trusting agent-sourced frame
timings — a check that belongs in preflight alongside the GPU check.

## One scenario, every target

The adapter SPI makes the *probe* portable. Making the *scenarios* portable is a
separate problem, and it is what `src/mcbench/targets.py` solves.

Commands are stable but not uniform. `/item replace block` is 1.17 and later;
before that it was `/replaceitem`. `/forceload` arrived in 1.13.1. `/tick warp`
is vanilla from 1.20.3, but on older versions it needs Carpet — which has no
build for plugin platforms at all. And Paper has no client, so a rendering
scenario cannot run there in any spelling.

Compiling one scenario for every target with a single hard-coded dialect would
therefore emit commands that fail, or quietly do nothing, on some of them. So
compilation takes a **target**:

```python
compile_plan(scenario, target=Target.parse("paper:1.21.1"))
```

A `Target` is `(platform, version, mods)`. A `Dialect` answers two questions
about it: *can this target do X*, and *how does it spell X*. The compiler asks
rather than assuming, so supporting an older version or a new platform changes
that one class and nothing else.

Requirements are **derived from what a scenario actually does**, not from a
hand-written list, so a scenario cannot forget to declare something and then
appear to run on a target that silently drops half of it.

The governing rule is the project's usual one: **a target that cannot express a
scenario refuses it, loudly, with a reason.** A scenario that half-executes
still produces numbers, and those numbers look entirely valid.

```
$ mcbench targets --with-mod carpet
                             fabric:1.21.1  neoforge:1.21.1  paper:1.21.1  paper:1.20.4
reference-hardware-baseline  ✓              ✓                ✗             ✗
entity-mobcap-saturation     ✓              ✓                ✓             ✓
...
paper:1.21.1: 8/11 runnable
    - paper is a server platform with no client, so rendering cannot be measured on it
```

### Versions

Both numbering schemes order in one sequence. Classic releases are `1.MAJOR.MINOR`
and releases from 2026 are year-based (`26.1.2`); because 26 > 1 a plain tuple
comparison already sorts the new scheme after every `1.x` with no special case.
Snapshots cannot be ordered against releases meaningfully and are treated as
unknown, which makes every capability gate fail closed — a clear refusal rather
than a command the target rejects.

### The pre-1.13 floor

Targets older than 1.13 are refused wholesale. The flattening renamed essentially
every block and item, so a scenario written with modern identifiers would build a
*different world* rather than fail — and that world would measure as though it
were correct. Supporting them needs an identifier mapping, which is a separate
piece of work; until then, refusing is the honest option.

## Evidence the SPI is the right size

Three adapters now exist across three unrelated ecosystems — Fabric and NeoForge
are mod loaders with clients, Paper is a server plugin platform with no client at
all — and all three are roughly the same length and shape. None contains a single
line of methodology. Everything that decides what a number means is in probe-core
and is shared between them verbatim.

NeoForge uses Mojang's official mappings while the Fabric adapter uses Yarn, so
the class names differ between two files that do the same thing. That divergence
is confined to those files, which is precisely what keeping the coupled surface
at three methods buys.

Their timing hooks differ only in how each platform exposes a tick. Fabric has a
tick event; Bukkit does not, but a repeating task scheduled at a one-tick period
runs exactly once per tick on the main thread, which is the same quantity. That
scheduler API has been stable since Bukkit's earliest releases — considerably
longer than any internal hook would have survived.

Paper also builds in well under a minute, because a plugin compiles against only
the API jar and never downloads Minecraft.

## Builds are separate, on purpose

`probe/` builds only `probe-core`. Each adapter under `probe/adapters/` is its
own standalone Gradle build, opted into by whoever wants a mod jar.

This is not tidiness. Building an adapter requires Fabric Loom — or the NeoForge
or Forge equivalent — which downloads and remaps Minecraft itself: hundreds of
megabytes, a network dependency, and a licence-sensitive step. `probe-core` has
zero Minecraft imports and its full test suite runs in about a second with no
network at all. Coupling the two would make every CI run of the methodology
tests hostage to a game download, and that fast, offline verification is one of
the most valuable properties the split buys.

It also isolates toolchain conflicts, which are real and unavoidable across a
matrix this wide. Fabric Loom 1.7.x calls Gradle's `Problems.forNamespace`,
removed in Gradle 8.13, so the Fabric adapter pins its own Gradle 8.10 wrapper
while `probe-core` builds on current Gradle. Under one build that would be an
impasse; under separate builds it is a line in a properties file. Every adapter
gets to track whatever toolchain its loader ecosystem requires.

## Adding a platform

1. Extend `ProbeAdapter`, implementing the three methods.
2. Call `onFrame()` from the render event, and report ticks by the best method
   the platform supports (see the table above).
3. Call `pump()` at the end of a tick, on a thread where commands are safe.
   Register it *after* the timing hooks so command execution lands outside the
   bracket. `pump()` also drives the setup phase and declares it finished, which
   is what starts warmup.
4. Register `finish()` as a shutdown hook.
5. Verify against the reference stream from
   `dev.mcbench.probe.core.SelfTest`, which needs no GPU, no account, and no
   working instance.

Step 5 is the reason the self-test exists. An adapter author can confirm their
stream is well-formed and their phase transitions are correct before ever
launching a real benchmark.

## Version matrix

Because the coupled surface is three methods, breadth is a build concern rather
than a code concern. Each adapter declares its target in its own
`gradle.properties`, so retargeting a version is normally a one-line change and
never touches Java. Where a version genuinely moves an API an adapter uses, the
divergence is isolated to that adapter rather than spreading into the core.

Mappings note: recent versions (26.x) have no Yarn mappings published, so
adapters build against **official Mojang mappings**, which are available for
every version and are the more durable choice regardless.
