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

1. a timing hook calling `onFrame()` or `onTick()`,
2. `executeCommand(String)`,
3. `requestShutdown()`.

That is the entire version-coupled surface. A new Minecraft version usually
needs **no code change** — only a rebuild against that version's mappings.

## Why the surface is this small

Three decisions do most of the work:

**Timing is measured between hook calls, not by wrapping the frame.** The
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
| NeoForge | `ClientTickEvent` / `ServerTickEvent` | planned |
| Forge | `TickEvent` | planned |
| Quilt | Fabric adapter loads directly | expected to work as-is |
| **Any version, any loader** | **JVM agent** | designed, see below |

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

Agent tradeoffs, stated up front: it needs a bytecode library (relocated to
avoid colliding with the game's own), and it measures presented frames, which on
a machine with vsync enabled measures the display rather than the renderer. The
harness must therefore verify vsync is off before trusting agent-sourced frame
timings — a check that belongs in preflight alongside the GPU check.

## Evidence the SPI is the right size

Fabric and Paper are unrelated ecosystems — one is a mod loader with a client,
the other a server plugin platform with no client at all — and their adapters
are nearly the same length and shape. Neither contains a single line of
methodology. Everything that decides what a number means is in probe-core and is
shared between them verbatim.

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
2. Call `onFrame()` / `onTick()` from the platform's tick or render event.
3. Call `pump()` at the end of a tick, on a thread where commands are safe.
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
