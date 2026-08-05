# Licensing and legal compliance

This document records the licence analysis that shaped mcbench's architecture.
Several design decisions that look like engineering choices are in fact legal
constraints. **Changing them can make the project unlawful to distribute.**

This is an engineering record, not legal advice.

## mcbench's own licence: Apache-2.0

A benchmark only becomes authoritative if the people being measured can adopt
it. Mod authors, modpack maintainers, hosting providers, and platforms all need
to be able to vendor, embed, and build on the harness without legal friction.
That rules out any copyleft licence for the harness itself.

Apache-2.0 over MIT for two reasons:

1. **Explicit patent grant.** A measurement standard should not leave adopters
   exposed to a contributor later asserting patents over a measurement
   technique. MIT is silent on patents; Apache-2.0 § 3 grants them and
   terminates the grant for anyone who sues.
2. **NOTICE mechanism.** Apache-2.0 § 4(d) gives a structured place to carry
   the third-party attribution and boundary notes that this project needs.

Apache-2.0 is one-way compatible with the MIT-licensed components we depend on
(MIT code may be incorporated into an Apache-2.0 work). It is **not** compatible
with GPLv2, and combining it with GPLv3 code produces a GPLv3 work, which is why
the spark boundary below matters.

## Hard constraint 1: Minecraft itself may not be redistributed

The [Minecraft EULA](https://www.minecraft.net/en-us/eula) and
[Usage Guidelines](https://www.minecraft.net/en-us/usage-guidelines) prohibit
redistributing the game or altered game files, and prohibit commercial
exploitation without permission.

**Architectural consequence:**

- mcbench never ships game jars, assets, libraries, or native binaries.
- Clients and servers are provisioned at runtime through official Mojang
  endpoints, driven by HeadlessMC, using the operator's own licensed account.
- Offline/headless accounts are used only for automated runs, which is the
  documented and permitted use of HeadlessMC in CI.
- A dedicated server will not start until the operator has accepted the EULA,
  and mcbench does not accept it for them. `eula.txt` is what Mojang reads to
  decide the operator agreed, so the tool writes it only after `--accept-eula`
  (or `accept_eula = true` in the suite manifest), and records the answer in
  the results provenance as `eula_accepted`. It used to write the file on every
  server run, with a comment claiming that running mcbench was itself the
  acceptance.
- Published mcbench results contain measurements and metadata only. They never
  embed game content.
- The project takes no payment for access to the game or to game content.

**Consequence for anyone hosting a public results service:** you are publishing
numbers you measured, which is fine. Do not let the service become a way for
users to obtain the game or mods.

## Hard constraint 2: mods under test may not be redistributed

Every mod is its own copyright work under its author's chosen licence. A great
many popular mods are all-rights-reserved. mcbench must therefore never mirror,
cache-and-serve, vendor, or commit mod jars.

**Architectural consequence:** mods are named in a manifest by
`(platform, project, version)` and resolved at runtime on the operator's own
machine into a gitignored working directory. The manifest, which is just a list
of identifiers, is the shareable and version-controllable artefact. The jars are
not.

### Modrinth (primary source)

The [Modrinth API](https://docs.modrinth.com/api/) is the default provider
because its terms suit automated use:

- 300 requests/minute per IP; the client must back off on `X-Ratelimit-*`.
- **A uniquely identifying `User-Agent` is mandatory.** Generic library agents
  risk being blocked. mcbench sends
  `mcbench/<version> (+https://github.com/<org>/mcbench)` and allows the
  operator to append contact details.
- Read-only project/version queries need no authentication.

### CurseForge (opt-in only, operator-supplied key)

The [CurseForge 3rd Party API Terms](https://support.curseforge.com/support/solutions/articles/9000207405-curse-forge-3rd-party-api-terms-and-conditions)
are materially stricter, and three clauses directly constrain us:

1. **Developers may not save or cache data obtained through the API.** So the
   CurseForge provider must not participate in mcbench's metadata cache. Its
   lookups are always live.
2. **Authors control third-party distribution per project.** Where a project
   disallows external distribution, the API does not return a download URL.
   mcbench must treat that as a clean, explanatory skip, never as a bug to route
   around, and never by scraping the website instead.
3. **The API may not be used to build a competing product.** mcbench measures
   performance; it is not a mod distribution platform or launcher. Keeping the
   CurseForge path opt-in and key-per-operator keeps that line clear.

Because of (1) and (2), CurseForge is **not** the default. Suites intended for
the public leaderboard should prefer Modrinth-hosted versions so that results
are reproducible by third parties.

## Hard constraint 3: benchmark worlds must be generated, not distributed

Worlds you create are yours, but distributing a world save that was produced by
someone else, or that embeds game content, is not safe ground.

**Architectural consequence:** a scenario never ships a world save. It ships a
*recipe*: a seed, a generator configuration, a spawn location, and a scripted
sequence of world edits and actions. The world is generated deterministically on
the operator's machine at run time.

A distributed world save would have been a reproducibility hazard regardless,
because it bakes in the generator version of whoever produced it. Generating
from a recipe makes the world's
provenance explicit and diffable.

## Hard constraint 4: the spark GPLv3 boundary

[spark](https://github.com/lucko/spark) is the ecosystem's standard profiler and
is an obvious thing to want to build on. It is **GPLv3**. Only its `spark-api`
submodule is MIT.

Linking Apache-2.0 code against GPLv3 code produces a combined work that must be
distributed under GPLv3. That would defeat the adoption goal set out at the top
of this document.

**The rule, stated so it survives future contributors:**

- mcbench **must never** add a compile or runtime dependency on `spark-core`,
  `spark-common`, or any spark platform module.
- Depending on `spark-api` (MIT) is permitted.
- Where an operator has spark installed, mcbench may interoperate only across a
  loose boundary: a separate process, a command invocation, or reading a file
  spark wrote. mcbench does not distribute spark and does not link it.
- mcbench's own instrumentation is written from scratch. Do not copy spark's
  source, including its sampling logic, as a "reference implementation".

Carpet is MIT and carries no such restriction; it may be depended on directly.

## Third-party licence summary

| Component | Licence | How mcbench uses it |
|---|---|---|
| Minecraft: Java Edition | Proprietary | Provisioned at runtime, never redistributed |
| Mods under test | Various, often ARR | Resolved at runtime, never redistributed |
| [HeadlessMC](https://github.com/3arthqu4ke/headlessmc) | MIT | External process for headless client runs |
| [mc-runtime-test](https://github.com/headlesshq/mc-runtime-test) | MIT | Prior art for CI patterns |
| [fabric-carpet](https://github.com/gnembon/fabric-carpet) | MIT | Optional runtime tick-warp workload driver |
| [packwiz](https://github.com/packwiz/packwiz) | MIT | Format inspiration for mod-set manifests |
| [spark](https://github.com/lucko/spark) | GPLv3 core / MIT api | **Never linked.** See § above |

## Checklist for contributors

Before adding a dependency or a data file, confirm:

- [ ] It is not a Minecraft game file or asset.
- [ ] It is not a mod jar, or any binary redistributed from a mod host.
- [ ] It is not a world save produced by anyone other than mcbench's generator.
- [ ] It is not GPL-licensed, or if it is, it is invoked across a process
      boundary and never linked or redistributed.
- [ ] Its licence permits redistribution under Apache-2.0, and it has been added
      to `NOTICE` and to the table above.
