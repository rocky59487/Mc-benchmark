"""The headless run loop.

Executes a :class:`~mcbench.planner.RunPlan` against real instances: provision,
launch, collect the probe stream, reduce to metrics, tear down, repeat. Designed
so a developer can get a verdict from one command and a CI job can consume the
result without parsing prose.

Instance provisioning is delegated to HeadlessMC as an external process. That
boundary is deliberate: HeadlessMC owns Mojang authentication and game
provisioning, so mcbench never touches credentials and never redistributes game
files (docs/LICENSING.md).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Platform, SuiteConfig, Variant
from ..metrics import (
    NS_PER_MS,
    RunFlag,
    RunMetrics,
    frame_cap_suspected,
    reduce_client_run,
    reduce_server_run,
)
from ..planner import Cell, PlannedRun, RunPlan, plan_runs
from ..providers import ModrinthClient, ModrinthError, ResolvedMod
from ..scenario import Scenario, Side
from ..stats import quantile_sketch
from ..targets import Target
from ..world import WorldError, create_world, fingerprint_world
from .launcher import KNOWN_FLAGS, LauncherCapabilities, probe_launcher
from .plan import check_target, write_plan
from .preflight import (
    Check,
    Preflight,
    Severity,
    competing_minecraft,
    run_preflight,
)
from .protocol import (
    AGENT_STREAM_NAME,
    ProbeError,
    ProbeStream,
    adopt_agent_frames,
    parse_probe_stream,
)

__all__ = [
    "RunOutcome",
    "HarnessError",
    "Harness",
    "resolve_variant_mods",
]


class HarnessError(RuntimeError):
    """The harness could not execute a run or a suite."""


def _spawn_of(scenario: Scenario) -> tuple[int, int, int]:
    """The scenario's declared spawn, or the vanilla default."""
    spawn = scenario.world.get("spawn")
    if not isinstance(spawn, dict):
        return (0, 64, 0)
    return tuple(int(spawn.get(axis, default))
                 for axis, default in (("x", 0), ("y", 64), ("z", 0)))


def _region_dirs(world: Path) -> list[Path]:
    """Every directory of terrain in a world, overworld and dimensions.

    Terrain only. A world directory also holds run state: `data/chunks.dat`
    records which chunks are force-loaded, and carrying that into the next run
    made `/forceload add` return "No chunks were marked for force loading",
    which the game reports as a failure and the probe as a failed setup
    command. Player data, POI and entities are run state too, and the scenario
    rebuilds them.
    """
    found = [world / "region"] if (world / "region").is_dir() else []
    for dimension in sorted(world.glob("DIM*")):
        if (dimension / "region").is_dir():
            found.append(dimension / "region")
    return found


def _one_or_many(per_variant: dict[str, Any]) -> Any:
    """Collapse a per-variant map that says the same thing for every variant.

    Provenance is read by people. A map repeating one value six times hides
    what it is recording behind its own shape.
    """
    values = set(per_variant.values())
    return values.pop() if len(values) == 1 else per_variant


def _spawn_chunk(scenario: Scenario) -> tuple[int, int]:
    """The chunk the scenario's spawn falls in, which the fingerprint centres on."""
    x, _, z = _spawn_of(scenario)
    return x >> 4, z >> 4


def _sha256(path: Path | None) -> str:
    """Hash an artefact, or "" when it cannot be read.

    Alongside the provider's SHA-512, which only proves the download matched
    the index. This identifies what was installed, local jars included.
    """
    import hashlib

    if path is None:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _java_version() -> str:
    """The JVM that will run the game, as it reports itself."""
    try:
        completed = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = (completed.stderr or completed.stdout or "").strip().splitlines()
    return output[0] if output else "unknown"


def _java_release(banner: str) -> str:
    """The release number out of a ``java -version`` banner.

    ``openjdk version "21.0.11" 2026-04-15 LTS`` is what the command prints;
    ``21.0.11`` is what ``System.getProperty("java.version")`` returns from
    inside the game. Comparing the two needs the part they have in common.
    """
    match = re.search(r'"([\d][\w.+\-]*)"', banner)
    return match.group(1) if match else ""


def _option_value(value: Any) -> str:
    """Render a value the way options.txt expects it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _repository_root() -> Path:
    """The checkout this module lives in, for locating built probe artefacts."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "probe").is_dir() and (parent / "docs").is_dir():
            return parent
    return Path.cwd()


#: The world name written into server.properties. Stated rather than left to the
#: server's default so the fingerprinter has a path it knows rather than one it
#: guesses.
SERVER_LEVEL_NAME = "mcbench"

#: Prefix for the world a client run is bootstrapped into. The probe starts on
#: SERVER_STARTED, so a client left at the title screen produces no stream.
CLIENT_LEVEL_NAME = "mcbench"

#: Frame cap written into options.txt. 260 is the top of vanilla's slider,
#: where the limiter is disabled. Written from a constant so the value is
#: explicit and recorded rather than left to whatever the instance defaults to.
#:
#: It is also the sentinel meaning "no limiter": a run is only checked against a
#: cap when a suite has set one below this. Checking every run against 260
#: flagged fast machines for rendering fast, which is the opposite of the
#: condition the flag exists to catch.
CLIENT_FPS_CAP = 260

#: Window size a client run renders at, as (width, height).
#:
#: Stated, because it decides what is being measured. Minecraft's own default is
#: 854x480, where a renderer is CPU-bound and a GPU sits idle; the same mod on
#: the same machine at 1080p can show a completely different result. Left to the
#: game, two runs on two machines would also render different numbers of pixels
#: and be pooled as though they had not.
#:
#: A suite may override it with a ``resolution`` game setting, as ``1280x720``.
#:
#: Verified from inside the game rather than by measuring the window from
#: outside it. A process that is not per-monitor DPI aware reads a window on a
#: 150% display as 1280x720 when it is 1920x1080, and the numbers it returns
#: look exactly like a launcher that ignored the request. Asking the client for
#: its framebuffer size is the only reading that cannot be wrong in that way.
CLIENT_RESOLUTION = (1920, 1080)



@dataclass
class RunOutcome:
    """What one executed run produced."""

    planned: PlannedRun
    metrics: RunMetrics | None
    stream: ProbeStream | None = None
    wall_clock_s: float = 0.0
    exit_code: int | None = None
    log_path: Path | None = None
    error: str = ""
    #: Hash over the block content of the world this run measured. Empty when the
    #: world could not be read, never a placeholder, because a fingerprint that
    #: two runs share only because both failed to compute one would silently
    #: certify exactly what it exists to check.
    world_fingerprint: str = ""
    #: Facts the probe reported from inside the game that contradict what the
    #: harness recorded for this run. One entry per disagreement, naming the
    #: field, the recorded value and the reported one.
    configuration_mismatches: list[str] = field(default_factory=list)
    #: Whether this run was handed a world or made one. The run that makes it
    #: measures the least settled terrain of any run of its scenario and is the
    #: one most likely to be refused for a fingerprint nothing else shares, so a
    #: reader needs to be able to tell that apart from a mod altering worldgen.
    world_restored: bool = False

    @property
    def succeeded(self) -> bool:
        return self.metrics is not None and self.metrics.admissible

    @property
    def status(self) -> str:
        """How this attempt ended, in one word.

        ``failed`` means no metrics at all: the launch died, timed out, or the
        probe never wrote a stream. ``inadmissible`` means it produced numbers
        the methodology refuses to pool.
        """
        if self.metrics is None:
            return "failed"
        return "completed" if self.metrics.admissible else "inadmissible"

    def to_record(self) -> dict[str, Any]:
        """Serialise this attempt, whether or not it produced a measurement.

        Every planned attempt appears in the results document: a seven-run cell
        with two failures must not serialise as a clean five-run cell.
        """
        record: dict[str, Any] = {
            "status": self.status,
            "position": self.planned.position,
            "replicate": self.planned.replicate,
            "round": self.planned.round_index,
            "values": dict(self.metrics.values) if self.metrics else {},
            "flags": [f.value for f in self.metrics.flags] if self.metrics else [],
            "wall_clock_s": round(self.wall_clock_s, 3),
            "world": self.world_fingerprint,
            "world_source": "restored" if self.world_restored else "generated",
        }
        if self.error:
            record["error"] = self.error
        if self.exit_code is not None:
            record["exit_code"] = self.exit_code
        if self.log_path is not None:
            record["log"] = str(self.log_path)
        if self.stream is not None and self.stream.summary:
            # Setup and warmup durations, which half of the warmup gate opened,
            # and the tick and allocation sources.
            record["probe"] = dict(self.stream.summary)
        if self.stream is not None and self.stream.metadata:
            # What the game said it was, from inside the game. The provenance
            # block says what the harness asked for, and the two are not the
            # same claim: a launcher free to pick its own Java or resolve its
            # own loader version can satisfy the request with something else.
            record["reported"] = dict(self.stream.metadata)
        if self.configuration_mismatches:
            record["configuration_mismatches"] = list(self.configuration_mismatches)
        if self.stream is not None and self.stream.client.frametimes_ns:
            # The shape of the distribution, not just its percentiles. Two
            # variants can share a mean and differ entirely in the tail, and a
            # reader given only summaries has to take the tail on trust.
            record["frametime_sketch_ms"] = [
                round(value, 4)
                for value in quantile_sketch(
                    [ns / NS_PER_MS for ns in self.stream.client.frametimes_ns]
                )
            ]
        return record


@dataclass
class ResolvedVariant:
    """A variant with every mod resolved to a concrete file on disk."""

    variant: Variant
    jars: list[Path] = field(default_factory=list)
    provenance: list[dict[str, str]] = field(default_factory=list)


class ProbeArtifacts:
    """The probe jar for a target, plus whatever that probe itself requires.

    The harness writes ``MCBENCH_PROBE_CONFIG`` and waits for a ``probe.jsonl``
    that only exists if something in the game reads it. This resolves that
    something for the selected platform, plus the Fabric API the probe
    hard-depends on. A missing artefact is a preflight blocker naming the build
    command rather than a suite that runs for two hours and produces nothing.
    """

    #: Where each platform's probe jar is built, relative to the repository.
    BUILD_PATHS = {
        "fabric": "probe/adapters/probe-fabric/build/libs",
        "neoforge": "probe/adapters/probe-neoforge/build/libs",
        "forge": "probe/adapters/probe-forge/build/libs",
        "paper": "probe/adapters/probe-paper/build/libs",
        "spigot": "probe/adapters/probe-paper/build/libs",
        "bukkit": "probe/adapters/probe-paper/build/libs",
    }

    #: Fabric's probe declares a hard dependency on Fabric API, so an instance
    #: without it loads the probe and then refuses to start it.
    FABRIC_API_PROJECT = "fabric-api"

    def __init__(
        self,
        loader: str,
        *,
        jar: Path | None = None,
        api_jar: Path | None = None,
    ) -> None:
        self.loader = loader
        self.jar = jar
        self.api_jar = api_jar

    @property
    def needs_fabric_api(self) -> bool:
        return self.loader == "fabric"

    @property
    def install_dir(self) -> str:
        """Where the platform loads its extensions from.

        Paper reads ``plugins/``. A plugin dropped into ``mods/`` is never
        loaded, and the server starts cleanly having measured vanilla.
        """
        return "plugins" if self.loader in ("paper", "spigot", "bukkit") else "mods"

    def missing(self) -> list[str]:
        """What still has to be provided before a run can produce a stream."""
        gaps: list[str] = []
        if self.jar is None or not self.jar.exists():
            gaps.append(
                f"the {self.loader} probe artefact. Build it with "
                f"`cd {self.BUILD_PATHS.get(self.loader, 'probe/adapters')} "
                f"&& ../gradlew build`, then pass --probe-jar."
            )
        if self.needs_fabric_api and (self.api_jar is None or not self.api_jar.exists()):
            gaps.append(
                "Fabric API, which the Fabric probe hard-depends on. Pass "
                "--fabric-api-jar, or let the harness resolve it from Modrinth."
            )
        return gaps

    def jars(self) -> list[Path]:
        found = [self.jar] if self.jar is not None else []
        if self.needs_fabric_api and self.api_jar is not None:
            found.append(self.api_jar)
        return [p for p in found if p.exists()]


def _resolve_local_jar(
    project: str, variant_name: str, local_root: Path | None
) -> Path:
    """Resolve a ``local:`` mod path, confined to ``local_root``.

    Suite manifests are untrusted input, since a public corpus or a CI job would
    benchmark suites submitted by other people, and this path is taken straight
    from one. Without confinement, ``../../../../etc/passwd`` reads an arbitrary
    host file and copies it into the instance.

    Absolute paths and traversal are both refused rather than normalised. The
    legitimate case, a developer benchmarking ``build/libs/mymod.jar``, is a
    relative path inside the root, and anyone who genuinely needs a file
    elsewhere can point ``--local-root`` at it deliberately, which makes the
    decision the operator's rather than the manifest's.
    """
    root = (local_root or Path.cwd()).resolve()
    candidate = (root / project).resolve()

    try:
        candidate.relative_to(root)
    except ValueError:
        raise HarnessError(
            f"{variant_name}: local mod path {project!r} resolves outside the "
            f"local root ({root}). Suite manifests are untrusted, so paths are "
            f"confined; point --local-root at the directory you mean instead."
        ) from None

    if not candidate.exists():
        raise HarnessError(f"{variant_name}: local mod not found: {candidate}")
    if not candidate.is_file():
        raise HarnessError(f"{variant_name}: local mod is not a file: {candidate}")

    # A mod is a zip. Checking the magic turns "the game mysteriously ignored
    # your mod" into an error that names the problem.
    with candidate.open("rb") as handle:
        if handle.read(2) != b"PK":
            raise HarnessError(
                f"{variant_name}: {candidate} is not a jar (no zip signature)"
            )
    return candidate


def resolve_variant_mods(
    variant: Variant,
    *,
    minecraft_version: str,
    loader: str,
    modrinth: ModrinthClient | None = None,
    local_root: Path | None = None,
) -> ResolvedVariant:
    """Resolve a variant's mods to files, recording provenance for each.

    Local jars are supported so a developer can benchmark a build that is not
    published yet, the common case during development. They are recorded with a
    file hash but marked ``platform="local"``, because a result depending on a
    jar nobody else can obtain is reproducible only on that machine and must not
    claim otherwise.
    """
    import hashlib

    resolved = ResolvedVariant(variant=variant)

    for mod in variant.mods:
        if mod.platform is Platform.LOCAL:
            candidate = _resolve_local_jar(mod.project, variant.name, local_root)
            digest = hashlib.sha512(candidate.read_bytes()).hexdigest()
            resolved.jars.append(candidate)
            resolved.provenance.append({
                "platform": "local",
                "project": candidate.name,
                "version": mod.version or "unknown",
                "sha512": digest,
                "path": str(candidate),
            })

        elif mod.platform is Platform.MODRINTH:
            if modrinth is None:
                raise HarnessError(
                    f"{variant.name}: a Modrinth client is required to resolve "
                    f"{mod.project}"
                )
            try:
                found: ResolvedMod = modrinth.resolve(
                    mod.project,
                    game_version=minecraft_version,
                    loader=loader,
                    version=mod.version,
                )
            except ModrinthError as exc:
                raise HarnessError(f"{variant.name}: {exc}") from exc
            resolved.jars.append(modrinth.download(found))
            resolved.provenance.append({
                "platform": "modrinth",
                "project": found.project_slug,
                "version": found.version_number,
                "sha512": found.sha512,
            })

        else:
            raise HarnessError(
                f"{variant.name}: platform {mod.platform.value!r} is not wired "
                f"up. CurseForge requires an operator-supplied API key and "
                f"cannot cache metadata; see docs/LICENSING.md."
            )

    return resolved


class Harness:
    """Executes suites headlessly.

    Args:
        suite: The validated suite configuration.
        scenarios: Scenario definitions, keyed by id.
        work_dir: Root for instances, logs, and probe output. Always gitignored.
        headlessmc: Path to the HeadlessMC jar or launcher binary.
        on_event: Optional callback for progress. Receives ``(event, payload)``
            so a CLI can render a progress bar and CI can emit structured logs
            from the same run.
    """

    def __init__(
        self,
        suite: SuiteConfig,
        scenarios: dict[str, Scenario],
        *,
        work_dir: str | Path = "work",
        headlessmc: str | Path | None = None,
        modrinth: ModrinthClient | None = None,
        local_root: str | Path | None = None,
        agent_jar: str | Path | None = None,
        probe_jar: str | Path | None = None,
        fabric_api_jar: str | Path | None = None,
        extra_launch_args: Sequence[str] = (),
        fresh_world: bool = False,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.suite = suite
        self.scenarios = scenarios
        self.work_dir = Path(work_dir)
        self.headlessmc = Path(headlessmc) if headlessmc else self._find_headlessmc()
        self.agent_jar = Path(agent_jar) if agent_jar else None
        self.modrinth = modrinth or ModrinthClient(
            cache_dir=self.work_dir / "cache" / "mods"
        )
        self.local_root = Path(local_root) if local_root else Path.cwd()
        self.on_event = on_event or (lambda event, payload: None)
        self._resolved: dict[str, ResolvedVariant] = {}
        self.probe = ProbeArtifacts(
            suite.loader.value,
            jar=Path(probe_jar) if probe_jar else self._find_probe_jar(),
            api_jar=Path(fabric_api_jar) if fabric_api_jar else None,
        )
        self._probe_provenance: list[dict[str, str]] = []
        #: Resolved on first use, then reused: the JVM on PATH does not change
        #: mid-suite, and asking it sixty times means sixty process spawns to
        #: learn the same string.
        self._java_release: str | None = None
        #: Whether the run being prepared was given a cached world or had to
        #: generate one. Set per run in _prepare_instance.
        self._world_restored = False
        #: Appended verbatim to every launch, for a launcher this does not know.
        self.extra_launch_args = list(extra_launch_args)
        #: Generate a world per run rather than sharing one per scenario.
        #:
        #: Sharing is the default because Minecraft's worldgen is not
        #: reproducible block for block, so per-run generation makes every pair
        #: of runs a fingerprint mismatch and nothing is ever pooled.
        #:
        #: Generating fresh is how a mod that alters worldgen shows up, since
        #: the fingerprint can only report a difference the run was allowed to
        #: produce. Use it to answer "does this mod change the world", not to
        #: compare frametimes.
        self.fresh_world = fresh_world
        self._capabilities: LauncherCapabilities | None = None

    # -- setup -----------------------------------------------------------

    @staticmethod
    def _find_headlessmc() -> Path | None:
        for name in ("headlessmc", "hmc"):
            if found := shutil.which(name):
                return Path(found)
        for candidate in (
            Path.home() / ".headlessmc" / "headlessmc-launcher.jar",
            Path("headlessmc-launcher.jar"),
        ):
            if candidate.exists():
                return candidate
        return None

    def _find_probe_jar(self) -> Path | None:
        """Locate a locally built probe for this platform, if there is one.

        For the common case of running from a checkout that just built it.
        Finding nothing returns None and the preflight blocker fires.
        """
        relative = ProbeArtifacts.BUILD_PATHS.get(self.suite.loader.value)
        if relative is None:
            return None
        for root in (Path.cwd(), _repository_root()):
            libs = root / relative
            if not libs.is_dir():
                continue
            # Prefer the shadowed/fat jar; the thin one is missing probe-core.
            jars = sorted(
                (p for p in libs.glob("*.jar")
                 if not p.name.endswith(("-sources.jar", "-javadoc.jar"))),
                key=lambda p: ("all" not in p.stem, p.name),
            )
            if jars:
                return jars[0]
        return None

    @property
    def needs_gpu(self) -> bool:
        """True when any selected scenario renders.

        Plugin platforms have no client at all, so a suite on Paper never needs a
        GPU regardless of what its scenarios declare, and demanding one would
        block a perfectly valid server benchmark.
        """
        if self.suite.loader.is_plugin_platform:
            return False
        return any(
            self.scenarios[s].side in (Side.CLIENT, Side.BOTH)
            for s in self.suite.scenarios
            if s in self.scenarios
        )

    def target(self, variant: Variant | None = None) -> Target:
        """The compile target for this suite, including the variant's mods.

        Mods matter because they grant capabilities: a tick-warp scenario is
        runnable on an older Fabric target only when Carpet is in the variant's
        mod list, and unrunnable in the baseline variant that has no mods.
        """
        mods = {m.project.lower() for m in variant.mods} if variant else set()
        return Target(
            platform=self.suite.loader,
            minecraft_version=self.suite.minecraft_version,
            mods=frozenset(mods),
        )

    def unsupported_scenarios(self, variant: Variant | None = None) -> list[str]:
        """Scenarios this suite's target cannot run, with reasons.

        Reported up front rather than discovered as an empty result. A client
        scenario dispatched to a Paper server records no frames; a tick-warp
        scenario on a target with no warp command silently measures at 20 TPS
        instead of exposing headroom. Both produce output that looks valid.
        """
        target = self.target(variant)
        unsupported = []
        for name in self.suite.scenarios:
            scenario = self.scenarios.get(name)
            if scenario is not None and check_target(scenario, target):
                unsupported.append(name)
        return unsupported

    def preflight(self, *, require_account: bool = True) -> Preflight:
        result = run_preflight(
            needs_gpu=self.needs_gpu,
            heap_mb=self.suite.heap_mb,
            require_account=require_account,
            work_dir=str(self.work_dir.parent if self.work_dir.parent.exists() else "."),
            launcher=self.headlessmc,
        )
        # Checked here rather than in preflight.py because it depends on how the
        # harness was constructed. Detecting it up front matters: without it a
        # suite launches, fails identically on every single run, and buries the
        # one real cause under dozens of duplicate errors.
        result.checks.append(
            Check("headlessmc", Severity.OK, f"found at {self.headlessmc}")
            if self.headlessmc is not None
            else Check(
                "headlessmc", Severity.BLOCK,
                "HeadlessMC not found; no instance can be launched",
                remedy=(
                    "Install HeadlessMC and put it on PATH, or pass --headlessmc "
                    "with the path to its jar. mcbench delegates game "
                    "provisioning and Mojang authentication to it so that it "
                    "never handles credentials or redistributes game files."
                ),
            )
        )

        # Flags the harness intends to use, checked against what the launcher
        # says it accepts. An unrecognised flag otherwise surfaces as a failed
        # launch minutes in, once per planned run.
        if self.headlessmc is not None:
            result.checks.append(self._launcher_check())

        # No probe means no measurement on any variant, baseline included.
        # Checked up front so the suite refuses before launching.
        gaps = self.probe.missing()
        result.checks.append(
            Check(
                "probe",
                Severity.OK,
                f"{self.suite.loader.value} probe at {self.probe.jar}"
                + (f", Fabric API at {self.probe.api_jar}"
                   if self.probe.needs_fabric_api and self.probe.api_jar else ""),
            )
            if not gaps
            else Check(
                "probe", Severity.BLOCK,
                "no probe artefact for this target; runs would produce no "
                "measurement stream at all",
                remedy=" ".join(gaps),
            )
        )
        return result

    def launcher_capabilities(self) -> LauncherCapabilities:
        """What the installed launcher accepts, probed once and cached."""
        if self._capabilities is None:
            self._capabilities = probe_launcher(self.headlessmc)
        return self._capabilities

    def resolve_all(self) -> dict[str, ResolvedVariant]:
        """Resolve every variant's mods up front.

        Done before the first launch on purpose: discovering a broken mod pin
        two hours into a suite wastes the whole suite, and the failure has
        nothing to do with the measurement.
        """
        self.resolve_probe()
        for variant in self.suite.variants:
            self.on_event("resolve.start", {"variant": variant.name})
            self._resolved[variant.name] = resolve_variant_mods(
                variant,
                minecraft_version=self.suite.minecraft_version,
                loader=self.suite.loader.value,
                modrinth=self.modrinth,
                local_root=self.local_root,
            )
            self.on_event("resolve.done", {
                "variant": variant.name,
                "jars": len(self._resolved[variant.name].jars),
            })
        return self._resolved

    def _launcher_check(self) -> Check:
        capabilities = self.launcher_capabilities()
        if not capabilities.probed:
            return Check(
                "launcher flags", Severity.INFO,
                f"could not read the launcher's help output ({capabilities.detail}); "
                f"assuming every flag is accepted",
                remedy=(
                    "If launches fail on an unrecognised argument, pass the "
                    "correct ones with --launch-arg."
                ),
            )
        if capabilities.unsupported_required:
            missing = ", ".join(capabilities.unsupported_required)
            return Check(
                "launcher flags", Severity.BLOCK,
                f"the launcher does not accept {missing}, without which a run "
                f"cannot be given its own instance directory or JVM arguments",
                remedy=(
                    "Use a HeadlessMC build that supports these, or point "
                    "--headlessmc at a wrapper script that translates them."
                ),
            )

        if not capabilities.speaks_flags:
            return Check(
                "launcher flags", Severity.OK,
                f"{capabilities.detail}; configured by property (hmc.gamedir, "
                f"hmc.gameargs) rather than by flag",
            )

        optional = capabilities.missing(
            frozenset(KNOWN_FLAGS) - set(capabilities.unsupported_required)
        )
        if optional:
            return Check(
                "launcher flags", Severity.INFO,
                "not accepted by this launcher and therefore dropped: "
                + ", ".join(f"{f} ({KNOWN_FLAGS[f]})" for f in optional if f in KNOWN_FLAGS),
            )
        return Check("launcher flags", Severity.OK, capabilities.detail)

    def resolve_probe(self) -> ProbeArtifacts:
        """Make sure the probe, and what the probe needs, is on disk.

        Fabric API is fetched from Modrinth when not supplied, and recorded in
        provenance like any other artefact: it is installed in every instance
        including the baseline, so it is part of what was measured.
        """
        if not self.probe.needs_fabric_api or self.probe.api_jar is not None:
            return self.probe

        self.on_event("resolve.start", {"variant": "<probe>"})
        try:
            found = self.modrinth.resolve(
                ProbeArtifacts.FABRIC_API_PROJECT,
                game_version=self.suite.minecraft_version,
                loader=self.suite.loader.value,
            )
        except ModrinthError as exc:
            # Not fatal: preflight reports the gap, and --fabric-api-jar
            # avoids needing the network at all.
            self.on_event("resolve.done", {
                "variant": "<probe>", "error": f"Fabric API: {exc}",
            })
            return self.probe

        self.probe.api_jar = self.modrinth.download(found)
        self._probe_provenance.append({
            "role": "probe_dependency",
            "platform": "modrinth",
            "project": found.project_slug,
            "version": found.version_number,
            "sha512": found.sha512,
        })
        self.on_event("resolve.done", {"variant": "<probe>", "jars": 1})
        return self.probe

    def provenance(self, preflight: Preflight | None = None) -> dict[str, Any]:
        """Everything needed to reconstruct what was actually launched.

        The *effective* configuration: resolved artefacts with hashes, the JVM
        arguments per variant, the probe and its dependencies, scenario content
        hashes, and the client frame cap by value. Host, Minecraft version and a
        publishability string are not enough to tell two different runs apart.
        """
        from .. import __version__

        artifacts: dict[str, list[dict[str, str]]] = {}
        for name, resolved in self._resolved.items():
            artifacts[name] = [
                {**entry, "filename": jar.name, "sha256": _sha256(jar)}
                for entry, jar in zip(resolved.provenance, resolved.jars, strict=True)
            ]

        payload: dict[str, Any] = {
            "mcbench": __version__,
            "minecraft_version": self.suite.minecraft_version,
            "loader": self.suite.loader.value,
            "loader_version": self.suite.loader_version or "unpinned",
            "preset": self.suite.preset.value,
            "heap_mb": self.suite.heap_mb,
            "java": _java_version(),
            "launcher": str(self.headlessmc) if self.headlessmc else "",
            "client_max_fps": CLIENT_FPS_CAP,
            # One string when every variant renders at the same size, which is
            # the normal case; a per-variant map only when a suite overrode it,
            # where the difference is the point.
            "client_resolution": _one_or_many({
                variant.name: "{}x{}".format(*self.effective_resolution(variant))
                for variant in self.suite.variants
            }),
            "artifacts": artifacts,
            "jvm_args": {
                variant.name: self.effective_jvm_args(variant)
                for variant in self.suite.variants
            },
            "game_settings": {
                variant.name: self.effective_game_settings(variant)
                for variant in self.suite.variants
            },
            "probe": {
                "install_dir": self.probe.install_dir,
                "jar": self.probe.jar.name if self.probe.jar else "",
                "sha256": _sha256(self.probe.jar) if self.probe.jar else "",
                "dependencies": list(self._probe_provenance),
                "agent": self.agent_jar.name if self.agent_jar else "",
            },
            "scenarios": {
                name: {
                    "version": scenario.version,
                    "content_hash": scenario.content_hash,
                    "pool_key": scenario.pool_key,
                }
                for name, scenario in self.scenarios.items()
                if name in self.suite.scenarios
            },
        }
        if preflight is not None:
            payload.update(preflight.host)
            payload["preflight_publishable"] = str(preflight.publishable)
        return payload

    def build_plan(self) -> RunPlan:
        return plan_runs(
            self.suite.scenarios,
            [v.name for v in self.suite.variants],
            runs_per_cell=self.suite.runs_per_cell,
            strategy=self.suite.order,
            seed=self.suite.seed,
        )

    # -- execution -------------------------------------------------------

    def _instance_dir(self, planned: PlannedRun) -> Path:
        """Absolute, because these paths cross into other processes.

        The launcher runs from its own directory and the game from the instance,
        so a relative path means something different in each. Neither reports an
        error when it resolves to the wrong place: the launcher creates an empty
        game directory, and the probe finds no config and stays inert, which is
        exactly what it is designed to do when it was not launched by mcbench.
        """
        return (
            self.work_dir / "instances"
            / f"{planned.cell.scenario}__{planned.cell.variant}__r{planned.replicate}"
        ).resolve()

    def _prepare_instance(
        self,
        planned: PlannedRun,
        scenario: Scenario,
        jars: Sequence[Path] | None = None,
    ) -> Path:
        """Create a clean instance directory with the variant's mods installed.

        Every run gets a *fresh* directory. Reusing one would carry over JIT
        profile data, the world, logs, and the OS page cache state, all of which
        make repeated runs correlated and cause the variance estimate to be too
        small, which is worse than no variance estimate because it looks
        rigorous.
        """
        instance = self._instance_dir(planned)
        if instance.exists():
            shutil.rmtree(instance)

        # Plugins go in plugins/, mods in mods/. Paper does not read mods/.
        install_dir = instance / self.probe.install_dir
        install_dir.mkdir(parents=True, exist_ok=True)

        if jars is None:
            resolved = self._resolved.get(planned.cell.variant)
            if resolved is None:
                raise HarnessError(
                    f"variant {planned.cell.variant!r} was not resolved; "
                    f"call resolve_all() first"
                )
            jars = resolved.jars

        # Into every instance, baseline included: without it there is no stream
        # to compare against.
        for jar in [*self.probe.jars(), *jars]:
            shutil.copy2(jar, install_dir / jar.name)

        probe_dir = instance / "mcbench"
        probe_dir.mkdir(parents=True, exist_ok=True)

        # Compile the scenario into commands the probe can execute. This is where
        # scenario interpretation lives: the probe receives a flat properties file
        # and two command lists, and needs no parser, no dependency, and no
        # knowledge of the methodology it is enforcing.
        variant = next(
            (v for v in self.suite.variants if v.name == planned.cell.variant), None
        )
        _, plan = write_plan(
            scenario,
            probe_dir,
            target=self.target(variant),
            preset=self.suite.preset,
            probe_output="mcbench/probe.jsonl",
        )

        # Render and simulation distance are not commands, so they go into the
        # instance config, with the variant's own game_settings layered on top.
        self._apply_instance_settings(
            instance, scenario,
            {**plan.instance_settings, **self.effective_game_settings(variant)},
        )

        # Kept for provenance and debugging: the plan is generated, and being able
        # to see what it was generated from is what makes a surprising result
        # diagnosable.
        (probe_dir / "scenario.json").write_text(
            json.dumps(
                {
                    "id": scenario.id,
                    "version": scenario.version,
                    "content_hash": scenario.content_hash,
                    "pool_key": scenario.pool_key,
                    "side": scenario.side.value,
                    "world": scenario.world,
                    "setup": list(scenario.setup),
                    "workload": list(scenario.workload),
                    "measurement": scenario.measurement,
                    "preset": self.suite.preset.value,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return instance

    def effective_game_settings(self, variant: Variant | None) -> dict[str, Any]:
        """Game settings actually applied to a variant's instances.

        The model is global settings, overridden per variant.
        """
        base = dict(self.suite.game_settings)
        if variant is not None:
            base.update(variant.game_settings)
        return base

    def effective_jvm_args(self, variant: Variant | None) -> list[str]:
        """JVM arguments actually applied to a variant's launches.

        The suite's arguments, then the variant's.
        """
        args = [f"-Xmx{self.suite.heap_mb}m", "-XX:+UseG1GC"]
        args.extend(self.suite.jvm_args)
        if variant is not None:
            args.extend(variant.jvm_args)
        return args

    def _apply_instance_settings(
        self, instance: Path, scenario: Scenario, settings: dict[str, Any]
    ) -> None:
        """Write settings that have no command equivalent into the instance's config."""
        if scenario.side in (Side.SERVER, Side.BOTH):
            simulation = settings.get("simulation_distance")
            view = settings.get("render_distance")
            lines = [
                f"level-seed={scenario.seed}",
                # Stated rather than defaulted, so the fingerprinter knows where
                # to look without inferring it from the server's conventions.
                f"level-name={SERVER_LEVEL_NAME}",
                "online-mode=false",
                "sync-chunk-writes=true",
            ]
            if simulation is not None:
                lines.append(f"simulation-distance={simulation}")
            if view is not None:
                lines.append(f"view-distance={view}")
            (instance / "server.properties").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            (instance / "eula.txt").write_text(
                "# The operator accepts the Minecraft EULA by running mcbench with a\n"
                "# licensed account; see docs/LICENSING.md.\neula=true\n",
                encoding="utf-8",
            )
            # A server got a seed and generated its own world every run, while
            # only the client half was given the cache. _restore_cached_world
            # says what that costs: worldgen is not reproducible block for
            # block, so every pair of runs is a fingerprint mismatch and nothing
            # is ever pooled. Eight of the eleven shipped scenarios are
            # server-side, and that is the state they were in.
            #
            # Terrain only. The server writes its own level.dat from
            # server.properties and then uses the region files it finds, which
            # is what makes the seed and the saved chunks agree.
            self._world_restored = self._restore_cached_world(
                scenario, instance / SERVER_LEVEL_NAME
            )

        if scenario.side in (Side.CLIENT, Side.BOTH):
            # Author the world quick-play enters; it must exist beforehand.
            world = create_world(
                instance / "saves",
                name=self._client_world_name(scenario),
                seed=scenario.seed,
                generator=str(scenario.world.get("generator", "default")),
                spawn=_spawn_of(scenario),
            )
            # Remembered because it explains a fingerprint that stands alone.
            # The run that generates a scenario's world is the one whose terrain
            # is least settled: chunks at the edge of generation keep changing
            # while their neighbours are made, so it can disagree with every run
            # that restores its cache, and the majority rule then refuses it.
            # That looks identical in the results to a mod altering worldgen.
            self._world_restored = self._restore_cached_world(scenario, world)

            options = instance / "options.txt"
            # Vsync off: a vsync-locked client measures the display, not the
            # renderer. maxFps is the top of vanilla's slider, which disables
            # the limiter. Written from a named constant and recorded, rather
            # than described as "uncapped".
            values: dict[str, Any] = {
                "maxFps": CLIENT_FPS_CAP,
                "enableVsync": "false",
                "graphicsMode": 1,
                "gamma": 1.0,
                "pauseOnLostFocus": "false",
            }
            if (render := settings.get("render_distance")) is not None:
                values["renderDistance"] = render
            # Whatever the suite declared wins, so an operator can deliberately
            # measure a capped or fancy-graphics configuration.
            for key, value in settings.items():
                if key not in ("render_distance", "simulation_distance"):
                    values[key] = value

            options.write_text(
                "\n".join(f"{k}:{_option_value(v)}" for k, v in values.items()) + "\n",
                encoding="utf-8",
            )

    def _launch_command(
        self,
        instance: Path,
        scenario: Scenario,
        variant: Variant | None = None,
    ) -> list[str]:
        if self.headlessmc is None:
            raise HarnessError(
                "HeadlessMC was not found. Install it and pass --headlessmc, or "
                "put 'headlessmc' on PATH. mcbench delegates game provisioning "
                "and Mojang authentication to HeadlessMC and never handles "
                "credentials or redistributes game files itself."
            )

        jvm_args = self.effective_jvm_args(variant)

        if self.agent_jar is not None and scenario.side.measures_frames:
            # Attached for rendering scenarios only. On a server run the agent
            # would find no frames to time and cost a transformer pass over
            # every class load for nothing.
            #
            # It is attached even where the loader has its own frame hook: the
            # agent instruments the buffer swap, which is the moment a frame is
            # actually presented, and the harness prefers it over an adapter's
            # frames when both arrive (adopt_agent_frames). Every variant in a
            # comparison gets the same treatment, which is what matters.
            if not self.agent_jar.exists():
                raise HarnessError(
                    f"agent jar not found: {self.agent_jar}. Build it with "
                    f"`cd probe/adapters/probe-agent && ./gradlew shadowJar`, "
                    f"or drop --agent-jar to measure frames through the "
                    f"loader adapter instead."
                )
            jvm_args.append(f"-javaagent:{self.agent_jar.resolve()}")

        capabilities = self.launcher_capabilities()
        if capabilities.accepts("--gamedir"):
            return self._flag_launch_command(
                instance, scenario, variant, jvm_args, capabilities
            )
        return self._headlessmc_launch_command(instance, scenario, variant, jvm_args)

    def _flag_launch_command(
        self,
        instance: Path,
        scenario: Scenario,
        variant: Variant | None,
        jvm_args: list[str],
        capabilities: LauncherCapabilities,
    ) -> list[str]:
        """A launcher that names the instance and the loader on its own flags."""
        assert self.headlessmc is not None
        if str(self.headlessmc).endswith(".jar"):
            command = ["java", "-jar", str(self.headlessmc)]
        else:
            command = [str(self.headlessmc)]

        command += ["launch", self.suite.minecraft_version]
        if capabilities.accepts("--loader"):
            command += ["--loader", self.suite.loader.value]
        command += [
            "--gamedir", str(instance),
            "--jvm", " ".join(jvm_args),
        ]
        # A pinned loader version has to be requested, or two runs on different
        # loader builds serialise as the same configuration.
        if self.suite.loader_version and capabilities.accepts("--loader-version"):
            command += ["--loader-version", str(self.suite.loader_version)]

        if scenario.side is Side.SERVER:
            command.append("--server")
        elif scenario.side in (Side.CLIENT, Side.BOTH):
            # Quick-play into the scenario's world, at a stated window size.
            # Without the first the client sits at the title screen, no
            # integrated server starts, and the probe never fires; without the
            # second it renders at whatever the game defaults to.
            game_args = self._game_arguments(scenario, variant)
            if capabilities.accepts("--quickPlaySingleplayer"):
                command += game_args
            else:
                # These are vanilla game arguments; a launcher that does not
                # name them may still forward what follows a bare `--`.
                command += ["--", *game_args]

        command += self.extra_launch_args
        return command

    def _headlessmc_launch_command(
        self,
        instance: Path,
        scenario: Scenario,
        variant: Variant | None,
        jvm_args: list[str],
    ) -> list[str]:
        """HeadlessMC 2.x, which configures by property rather than by flag.

        Its `launch` takes a version id and `--jvm`, and nothing else this
        harness needs: the instance directory is `hmc.gamedir`, extra game
        arguments are `hmc.gameargs`, and the loader is selected by naming the
        loader's own version id rather than by a `--loader` flag. Everything
        else here is the same run.

        `-quit` is deliberately not passed. It returns as soon as the game is
        spawned, and the harness has to wait for the process it is timing.
        """
        assert self.headlessmc is not None
        if scenario.side is Side.SERVER:
            # HeadlessMC 2.x drives servers through a separate `server` command
            # with its own registration step, not through a flag on `launch`.
            # Refusing is the point: without one, `launch` starts the client,
            # the client run succeeds, and a server suite reports frametimes
            # for a scenario that was supposed to measure tick cost.
            raise HarnessError(
                f"{scenario.id} is a server scenario, and this launcher has no "
                f"--server flag. mcbench cannot yet drive HeadlessMC's own "
                f"'server' command, so run server scenarios through a launcher "
                f"that accepts --server, or restrict this suite with "
                f"--side client."
            )
        # Absolute, because the launcher runs from its own directory and would
        # otherwise resolve a relative instance path against that. It does not
        # fail when it does: it creates the directory, finds no mods and no
        # world in it, and leaves the game sitting at the title screen.
        properties = [
            f"-Dhmc.gamedir={instance.resolve()}",
            # One command, then exit, rather than an interactive shell on stdin.
            "-Dhmc.exit.on.failed.command=true",
        ]
        # JVM arguments go in the property rather than in `--jvm` inside the
        # command string. HeadlessMC splits that string on whitespace and
        # `--jvm` takes a single token, so a second argument became a stray
        # positional and the rest of the launch was parsed as something else:
        # the game started, at the title screen, having ignored quick-play.
        if jvm_args:
            properties.append(f"-Dhmc.jvmargs={' '.join(jvm_args)}")
        if game_args := self._game_arguments(scenario, variant):
            properties.append(f"-Dhmc.gameargs={' '.join(game_args)}")

        launch = ["launch", self._headlessmc_version_id(), *self.extra_launch_args]
        return [
            "java", *properties, "-jar", str(self.headlessmc),
            "--command", " ".join(launch),
        ]

    def _launch_cwd(self, instance: Path) -> Path:
        """Where to run the launcher from.

        HeadlessMC finds its own state, including the account it logged in
        with, relative to the working directory. Running it from the instance
        made it create an empty state directory there and refuse the launch
        with "you can't play the game without an account", once per run, on a
        machine that was logged in. The instance is named by hmc.gamedir, so
        the working directory is free to be the launcher's own.
        """
        if self.headlessmc is not None and not self.launcher_capabilities().speaks_flags:
            launcher = self.headlessmc
            return launcher.parent if launcher.is_file() else launcher
        return instance

    def _headlessmc_version_id(self) -> str:
        """The version HeadlessMC installed for this suite's loader.

        HeadlessMC lists a modded install under the loader's own id, such as
        `fabric-loader-0.19.3-1.21.1`. Asking for the plain Minecraft version
        would launch vanilla, and every mod under test would be absent.
        """
        version = self.suite.minecraft_version
        loader = self.suite.loader.value
        if loader in ("vanilla", ""):
            return version
        if self.suite.loader_version:
            return f"{loader}-loader-{self.suite.loader_version}-{version}"
        return f"{loader}-loader-{version}"

    def _game_arguments(
        self, scenario: Scenario, variant: Variant | None = None
    ) -> list[str]:
        """Vanilla game arguments a client run needs."""
        if scenario.side is Side.SERVER:
            return []
        width, height = self.effective_resolution(variant)
        return [
            "--quickPlaySingleplayer", self._client_world_name(scenario),
            "--width", str(width),
            "--height", str(height),
        ]

    def effective_resolution(self, variant: Variant | None) -> tuple[int, int]:
        """The window size a variant renders at.

        Declared as ``resolution = "1280x720"`` in a suite's game settings, or
        the harness default. Unparseable values are refused rather than ignored:
        silently falling back would mean a suite that asked for 1440p and got
        854x480, which is a different measurement wearing the same name.
        """
        declared = self.effective_game_settings(variant).get("resolution")
        if declared is None:
            return CLIENT_RESOLUTION
        text = str(declared).lower().replace(" ", "")
        match = re.fullmatch(r"(\d{3,5})x(\d{3,5})", text)
        if not match:
            raise HarnessError(
                f"resolution {declared!r} is not WIDTHxHEIGHT, as in '1920x1080'"
            )
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _client_world_name(scenario: Scenario) -> str:
        """The save directory a client run enters.

        Named after the scenario so an instance holding more than one save is
        unambiguous, and ``_world_dir`` knows which to fingerprint.
        """
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in scenario.id)
        return f"{CLIENT_LEVEL_NAME}-{safe}" if safe else CLIENT_LEVEL_NAME

    def measure_subset(
        self,
        scenario_id: str,
        mods: Sequence[str],
        replicate: int,
        *,
        metric: str,
        jars_by_id: dict[str, Path],
        timeout_s: float | None = None,
    ) -> float | None:
        """Run one replicate with an arbitrary mod subset; return one metric.

        This is the measurement primitive behind ``mcbench bisect``. A bisect
        probes subsets that are not declared variants, so it cannot go through
        the suite's variant table.

        Returns None when the run fails, which the oracle drops rather than
        substitutes, because inventing a value would fabricate a measurement.
        """
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            raise HarnessError(f"unknown scenario {scenario_id!r}")

        missing = [m for m in mods if m not in jars_by_id]
        if missing:
            raise HarnessError(
                f"no resolved jar for: {', '.join(sorted(missing))}"
            )

        label = "+".join(sorted(mods)) or "baseline"
        # Hashed so a long subset name cannot overflow the filesystem's path
        # limit halfway through a multi-hour search.
        import hashlib

        digest = hashlib.sha256(label.encode()).hexdigest()[:12]
        planned = PlannedRun(
            cell=Cell(scenario_id, f"probe-{digest}"),
            replicate=replicate,
            position=replicate,
            round_index=replicate,
        )
        outcome = self.execute_run(
            planned,
            timeout_s=timeout_s,
            jars=[jars_by_id[m] for m in mods],
        )
        if outcome.metrics is None or not outcome.metrics.admissible:
            return None
        return outcome.metrics.values.get(metric)

    def execute_run(
        self,
        planned: PlannedRun,
        *,
        timeout_s: float | None = None,
        jars: Sequence[Path] | None = None,
    ) -> RunOutcome:
        """Execute one run end to end."""
        scenario = self.scenarios.get(planned.cell.scenario)
        if scenario is None:
            return RunOutcome(
                planned=planned, metrics=None,
                error=f"unknown scenario {planned.cell.scenario!r}",
            )

        self.on_event("run.start", {
            "position": planned.position,
            "cell": str(planned.cell),
            "replicate": planned.replicate,
        })

        instance = self._prepare_instance(planned, scenario, jars)
        log_path = instance / "mcbench" / "instance.log"
        probe_path = instance / "mcbench" / "probe.jsonl"
        agent_path = instance / "mcbench" / AGENT_STREAM_NAME

        if timeout_s is None:
            # Generous headroom over the scenario's own budget: provisioning,
            # worldgen, and JVM startup are untimed but real, and killing a run
            # that was merely slow to start would bias against heavier mods.
            timeout_s = scenario.duration(self.suite.preset) * 4 + 900

        started = time.monotonic()
        exit_code: int | None = None
        error = ""

        variant = next(
            (v for v in self.suite.variants if v.name == planned.cell.variant), None
        )
        try:
            command = self._launch_command(instance, scenario, variant)
        except HarnessError as exc:
            # Emit the failure rather than returning quietly: a run that dies
            # without saying why leaves the operator staring at a progress line
            # that never completes.
            self.on_event("run.fail", {"cell": str(planned.cell), "error": str(exc)})
            return RunOutcome(planned=planned, metrics=None, error=str(exc))

        # These names are the contract with ProbeSession.fromEnvironment(); the
        # probe stays completely inert unless MCBENCH_PROBE_CONFIG is set, so it
        # is harmless left installed in a normal play session.
        environment = dict(os.environ)
        environment["MCBENCH_PROBE_CONFIG"] = str(instance / "mcbench" / "probe.properties")
        environment["MCBENCH_PROBE_OUTPUT"] = str(probe_path)
        # Stated rather than derived. The agent would work this location out for
        # itself, but making it an explicit part of the launch environment means
        # the two sides cannot drift into writing and reading different files,
        # which has happened once already on this seam.
        environment["MCBENCH_AGENT_OUTPUT"] = str(agent_path)

        launch_cwd = self._launch_cwd(instance)
        # Sampled with our own game not yet started, and again once it has
        # exited, so what is found is somebody else's. See _competing_during().
        competitors = self._competing_now()
        try:
            with log_path.open("w", encoding="utf-8") as log:
                # The exact invocation, so a run that behaved oddly can be
                # reproduced by hand from its own log rather than reconstructed
                # from the suite and the harness's defaults.
                log.write(f"# cwd: {launch_cwd}\n")
                log.write(f"# command: {subprocess.list2cmdline(command)}\n")
                for name in ("MCBENCH_PROBE_CONFIG", "MCBENCH_PROBE_OUTPUT"):
                    log.write(f"# {name}={environment[name]}\n")
                log.flush()
                completed = subprocess.run(
                    command, cwd=launch_cwd, env=environment,
                    stdout=log, stderr=subprocess.STDOUT,
                    timeout=timeout_s, check=False,
                )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            error = f"run exceeded its {timeout_s:.0f}s timeout"
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"failed to launch: {exc}"

        wall_clock = time.monotonic() - started
        competitors = self._competing_during(competitors)

        if error and not probe_path.exists():
            self.on_event("run.fail", {"cell": str(planned.cell), "error": error})
            return RunOutcome(
                planned=planned, metrics=None, wall_clock_s=wall_clock,
                exit_code=exit_code, log_path=log_path, error=error,
            )

        try:
            stream = parse_probe_stream(probe_path)
        except ProbeError as exc:
            self.on_event("run.fail", {"cell": str(planned.cell), "error": str(exc)})
            return RunOutcome(
                planned=planned, metrics=None, wall_clock_s=wall_clock,
                exit_code=exit_code, log_path=log_path, error=str(exc),
            )

        self._adopt_agent_stream(stream, agent_path, planned)

        metrics = self._reduce(
            stream,
            scenario,
            max_fps=float(
                self.effective_game_settings(variant).get("maxFps", CLIENT_FPS_CAP)
            ),
        )
        if competitors:
            metrics.flags.append(RunFlag.ENVIRONMENT_NOISY)
            self.on_event("run.noisy", {
                "cell": str(planned.cell), "processes": len(competitors),
            })

        disagreements = self._configuration_mismatches(stream, scenario, variant)
        mismatches = [
            f"{field_name}: recorded {recorded}, game reported {reported}"
            for field_name, recorded, reported in disagreements
        ]
        if any(f in self.DISQUALIFYING_FIELDS for f, _, _ in disagreements):
            metrics.flags.append(RunFlag.CONFIGURATION_MISMATCH)

        # Keep the first run's terrain so every later run of this scenario
        # measures the same one, whatever the generator did this time.
        #
        # Unconditional, and before the fingerprint. This used to happen inside
        # _fingerprint_world, which is skipped whenever the probe reported a
        # fingerprint of its own — so on any platform whose probe does report
        # one, no world was ever cached, none was ever shared, and every pair of
        # runs was a fingerprint mismatch. It works today only because no
        # shipped adapter implements it.
        if (saved := self._world_dir(instance, scenario)) is not None:
            self._cache_world(scenario, saved)

        # The probe may report its own fingerprint over the live world; the
        # harness's is computed from the save on disk and needs no game API, so
        # it works on every version and platform. Where both exist the probe's
        # is preferred, being taken while the measurement region was loaded.
        fingerprint = (
            stream.world_fingerprint
            or self._fingerprint_world(instance, scenario, planned)
        )
        self.on_event("run.done", {
            "cell": str(planned.cell),
            "wall_clock_s": round(wall_clock, 1),
            "admissible": metrics.admissible,
            "flags": [f.value for f in metrics.flags],
        })
        # After run.done, which closes the progress line this would otherwise
        # be printed into the middle of.
        if mismatches:
            self.on_event("run.mismatch", {
                "cell": str(planned.cell), "fields": mismatches,
            })

        return RunOutcome(
            planned=planned, metrics=metrics, stream=stream,
            wall_clock_s=wall_clock, exit_code=exit_code, log_path=log_path,
            error=error, world_fingerprint=fingerprint,
            configuration_mismatches=mismatches,
            world_restored=self._world_restored,
        )

    def _world_cache(self, scenario: Scenario) -> Path:
        """Where a scenario's generated terrain is kept between runs."""
        return self.work_dir / "worlds" / self._client_world_name(scenario)

    @property
    def shares_world(self) -> bool:
        """Whether every run of a scenario measures one generated world."""
        return not self.fresh_world

    def _restore_cached_world(self, scenario: Scenario, world: Path) -> bool:
        """Give this run the terrain an earlier run of the scenario generated.

        Minecraft's worldgen is not reproducible block for block. Two runs of
        one seed agree on terrain shape and biomes and disagree on where a
        handful of ore blobs and plants landed, because feature placement
        depends on the order neighbouring chunks were generated in, which is
        threaded. Measured here: 109 of 289 chunks differed between two runs,
        by 1 to 9 blocks each, andesite against granite and water against
        seagrass.

        A per-run world therefore makes METHODOLOGY section 7 unsatisfiable,
        since every pair of runs is a fingerprint mismatch and nothing is ever
        pooled. Generating once and reusing it is what makes runs comparable:
        the fingerprint then goes back to checking that the reuse worked, and
        to catching a mod that alters worldgen.

        Nothing is redistributed. The terrain is generated on this machine and
        stays under the working directory (docs/LICENSING.md).
        """
        cached = self._world_cache(scenario)
        if not self.shares_world or not (cached / "region").is_dir():
            return False
        for source in _region_dirs(cached):
            shutil.copytree(
                source, world / source.relative_to(cached), dirs_exist_ok=True
            )
        return True

    def _cache_world(self, scenario: Scenario, world: Path) -> None:
        """Keep this run's terrain for every later run of the scenario."""
        cached = self._world_cache(scenario)
        if not self.shares_world:
            return
        if (cached / "region").is_dir() or not (world / "region").is_dir():
            return
        cached.mkdir(parents=True, exist_ok=True)
        for source in _region_dirs(world):
            shutil.copytree(
                source, cached / source.relative_to(world), dirs_exist_ok=True
            )
        self.on_event("world.cached", {
            "scenario": scenario.id, "path": str(cached),
        })

    def _world_dir(self, instance: Path, scenario: Scenario) -> Path | None:
        """Where this run's world was saved, or None if it cannot be located.

        A dedicated server writes to the ``level-name`` directory; a client
        writes under ``saves/``. Ambiguity is reported rather than resolved by
        picking one.
        """
        if scenario.side is Side.SERVER:
            candidate = instance / SERVER_LEVEL_NAME
            return candidate if candidate.is_dir() else None

        saves = instance / "saves"
        if not saves.is_dir():
            return None
        # The harness authors the save, so the name is known.
        expected = saves / self._client_world_name(scenario)
        if (expected / "region").is_dir():
            return expected

        worlds = [d for d in sorted(saves.iterdir()) if (d / "region").is_dir()]
        return worlds[0] if len(worlds) == 1 else None

    def _client_world_mismatch(
        self, instance: Path, scenario: Scenario, world_dir: Path
    ) -> str:
        """Why the client's world is not the one authored, or "".

        The ``level.dat`` the harness writes is only a request. A client that
        rejects it creates its own world with its own seed, and a run over that
        is a measurement of different terrain that would otherwise look
        perfectly normal.
        """
        saves = instance / "saves"
        expected = saves / self._client_world_name(scenario)
        if world_dir != expected:
            return f"the client used {world_dir.name}, not {expected.name}"

        others = [
            d for d in sorted(saves.iterdir())
            if d.is_dir() and d != expected and (d / "level.dat").exists()
        ] if saves.is_dir() else []
        if others:
            return (
                f"the client created {', '.join(d.name for d in others)} "
                f"alongside the authored world"
            )

        try:
            import gzip

            from ..nbt import parse_nbt

            data = parse_nbt(
                gzip.decompress((expected / "level.dat").read_bytes())
            )["Data"]
        except (OSError, KeyError, ValueError, TypeError):
            return "the world's level.dat could not be read back"

        settings = data.get("WorldGenSettings")
        seed = settings.get("seed") if isinstance(settings, dict) else data.get("RandomSeed")
        if seed is not None and int(seed) != scenario.seed:
            return f"the world's seed is {seed}, not the scenario's {scenario.seed}"
        return ""

    def _fingerprint_world(
        self, instance: Path, scenario: Scenario, planned: PlannedRun
    ) -> str:
        """Hash the world this run measured, for the pooling check.

        METHODOLOGY §7: runs whose worlds differ are never pooled. This is what
        makes that enforceable; without it the claim is a promise the code does
        not keep.

        Failure to compute one is reported and returns empty. Substituting a
        placeholder would let two failed runs "agree" and be pooled on the
        strength of a check that never ran.
        """
        world_dir = self._world_dir(instance, scenario)
        if world_dir is None:
            self.on_event("run.world", {
                "cell": str(planned.cell), "result": "no world directory found",
            })
            return ""

        if scenario.side in (Side.CLIENT, Side.BOTH):
            mismatch = self._client_world_mismatch(instance, scenario, world_dir)
            if mismatch:
                # The client did not enter the world the harness authored. It
                # created its own, or regenerated ours from a level.dat it
                # refused. Either way the seed and generator are the game's
                # choice and this is not the scenario that was requested.
                self.on_event("run.world", {
                    "cell": str(planned.cell), "result": mismatch,
                })
                return ""
        # Only the region the scenario declared. Hashing every saved chunk made
        # the result depend on how far a run's chunk loading happened to reach.
        try:
            result = fingerprint_world(
                world_dir,
                radius_chunks=scenario.fingerprint_radius_chunks,
                centre=_spawn_chunk(scenario),
            )
        except (WorldError, OSError) as exc:
            self.on_event("run.world", {
                "cell": str(planned.cell), "result": f"unreadable: {exc}",
            })
            return ""

        self.on_event("run.world", {
            "cell": str(planned.cell),
            "result": str(result),
            "sha256": result.sha256,
            "complete": result.complete,
            "chunks": result.chunks,
        })
        # An incomplete read is not a fingerprint. Two worlds that both failed on
        # the same region would hash identically over what was left, and two that
        # generated no terrain would both hash to the digest of nothing.
        return result.sha256 if result.usable else ""

    @staticmethod
    def _competing_now() -> list[str]:
        """Minecraft processes that are not ours, right now.

        An unreadable process table yields nothing rather than a warning. The
        preflight already reports that it could not enumerate processes, once,
        where an operator will read it; repeating it per run would say the same
        thing sixty times and make the flag mean "we did not look".
        """
        return competing_minecraft() or []

    def _competing_during(self, before: list[str]) -> list[str]:
        """Competitors seen either side of a run, deduplicated.

        Preflight checks this once, and a suite runs for hours. Something
        started after it passed is invisible to it and is the most common way a
        careful benchmark is quietly ruined, so every run asks again.

        Sampled either side of the launch rather than during it, for two
        reasons: our own game is a Minecraft process and could not be told from
        anyone else's, and enumerating the process table costs real CPU on
        Windows, where it means a PowerShell CIM query. A competitor that both
        started and exited inside one run is therefore missed. That is a gap in
        what this detects, not a claim that the run was clean.
        """
        seen = {line: None for line in before}
        seen.update(dict.fromkeys(self._competing_now()))
        return list(seen)

    def _java_release_on_path(self) -> str:
        """The release number of the JVM this harness would launch."""
        if self._java_release is None:
            self._java_release = _java_release(_java_version())
        return self._java_release

    #: Fields whose disagreement means a different experiment ran, rather than
    #: the same one in a differently-described environment. A run that measured
    #: another scenario, another Minecraft or another loader cannot be pooled
    #: with runs that did not.
    #:
    #: The JVM and the window size are outside it. Both are properties of the
    #: launch that every variant shares, so a wrong one leaves the comparison
    #: intact and only makes the published description wrong; the record carries
    #: what was asked for and what happened, and the reader can see both.
    #:
    #: Variants disagreeing with *each other* about either would be a real
    #: confound rather than a mis-description, and is not detected here. It
    #: cannot be read off a single run, and the rule needs care: a suite may
    #: legitimately declare a different resolution per variant, so uniformity
    #: has to be required within a declared resolution rather than across all.
    DISQUALIFYING_FIELDS = frozenset({
        "platform", "scenario", "scenario_version", "scenario_hash",
        "minecraft_version", "loader_version",
    })

    def _configuration_mismatches(
        self, stream: ProbeStream, scenario: Scenario, variant: Variant | None = None
    ) -> list[tuple[str, str, str]]:
        """Facts the game reported that contradict what this run recorded.

        The probe's opening event names the platform, scenario, Minecraft
        version, loader version and JVM the game actually had. ``provenance()``
        names what the suite asked for and what the harness's own ``java``
        reported. Nothing compared the two, and they are not the same claim: a
        launcher is free to satisfy the request with a different JVM, resolve a
        different loader build, or start from a scenario file edited since the
        plan was made. Each of those yields a results document describing a run
        that did not happen, with no sign anywhere that it did not.

        Fields the probe omits are not disagreements. Older probes and platforms
        without the concept stay silent rather than guess, and reading silence
        as contradiction would flag every run on them.

        Returns ``(field, recorded, reported)`` per disagreement.
        """
        if not stream.metadata:
            return []

        expected: list[tuple[str, str]] = [
            ("platform", self.suite.loader.value),
            ("scenario", scenario.id),
            ("scenario_version", scenario.version),
            ("scenario_hash", scenario.content_hash),
            ("minecraft_version", self.suite.minecraft_version),
            ("loader_version", self.suite.loader_version or ""),
            ("java", self._java_release_on_path()),
        ]
        if scenario.side is Side.CLIENT:
            expected.append(
                ("window", "{}x{}".format(*self.effective_resolution(variant)))
            )
        return [
            (field_name, recorded, reported)
            for field_name, recorded in expected
            if (reported := stream.metadata.get(field_name, ""))
            and recorded
            and reported != recorded
        ]

    def _adopt_agent_stream(
        self, stream: ProbeStream, agent_path: Path, planned: PlannedRun
    ) -> None:
        """Take frame timings from the JVM agent when the adapter had none.

        Absence of the file is the normal case, since the agent is only present
        when an operator added ``-javaagent`` to the launch, so it is not an
        error.
        A file that is present but unreadable is: it means the agent ran and
        something went wrong, and silently proceeding would report an
        adapter-only result as though nothing had been attempted.
        """
        if not agent_path.exists():
            return
        try:
            agent = parse_probe_stream(agent_path)
        except ProbeError as exc:
            self.on_event("run.agent", {
                "cell": str(planned.cell), "result": f"unreadable: {exc}",
            })
            return
        self.on_event("run.agent", {
            "cell": str(planned.cell), "result": adopt_agent_frames(stream, agent),
        })

    @staticmethod
    def _reduce(
        stream: ProbeStream, scenario: Scenario, *, max_fps: float = CLIENT_FPS_CAP
    ) -> RunMetrics:
        """Reduce a probe stream to metrics, merging client and server samples.

        A stream that reported errors is reduced and then marked inadmissible:
        the numbers are worth seeing while diagnosing, but they describe a world
        the scenario never built and look entirely ordinary.
        """
        stream.server.saturated = stream.server.saturated or scenario.saturated
        stream.server.measures_execution = stream.tick_source.measures_execution
        stream.client.real_allocation = stream.real_allocation
        stream.server.real_allocation = stream.real_allocation

        if scenario.side is Side.SERVER:
            metrics = reduce_server_run(stream.server)
        elif scenario.side is Side.CLIENT:
            metrics = reduce_client_run(stream.client)
        else:
            client = reduce_client_run(stream.client)
            server = reduce_server_run(stream.server)
            # Server keys never collide with client keys in the registry, so a
            # merge is unambiguous for integrated runs.
            metrics = RunMetrics(
                values={**client.values, **server.values},
                flags=list({*client.flags, *server.flags}),
                sample_count=client.sample_count + server.sample_count,
            )

        for flag in stream.flags:
            if flag not in metrics.flags:
                metrics.flags.append(flag)

        # A stream carrying errors is not a measurement of the scenario asked
        # for, whatever numbers it also carries.
        if stream.errors and RunFlag.PROBE_ERROR not in metrics.flags:
            metrics.flags.append(RunFlag.PROBE_ERROR)

        period_only = (
            not stream.tick_source.measures_execution
            and stream.has_server_data
            and RunFlag.TICK_PERIOD_ONLY not in metrics.flags
        )
        if period_only:
            metrics.flags.append(RunFlag.TICK_PERIOD_ONLY)

        # Only where a limiter is actually configured. At CLIENT_FPS_CAP there
        # is none, and asking whether frames came in under 3.8 ms then just asks
        # whether the machine is fast.
        if (
            scenario.side.measures_frames
            and stream.client.frametimes_ns
            and max_fps < CLIENT_FPS_CAP
        ):
            frames_ms = [ns / 1_000_000.0 for ns in stream.client.frametimes_ns]
            # The client sitting against its own limiter means what was measured
            # is the cap. Every variant would score it and the comparison would
            # confidently report equivalence.
            capped = (
                frame_cap_suspected(frames_ms, max_fps)
                and RunFlag.FRAME_CAP_SUSPECTED not in metrics.flags
            )
            if capped:
                metrics.flags.append(RunFlag.FRAME_CAP_SUSPECTED)

        return metrics

    def run_suite(
        self, *, stop_on_failure: bool = False, timeout_s: float | None = None
    ) -> list[RunOutcome]:
        """Execute the whole plan in its scheduled order.

        The order is the plan's order and is never re-sorted for convenience,
        because it is the fairness guarantee (planner.py). Runs that fail are
        recorded
        and the suite continues by default, because one bad run should not
        discard the hours already spent on the rest.
        """
        plan = self.build_plan()
        self.on_event("suite.start", {
            "runs": len(plan), "strategy": plan.strategy.value, "seed": plan.seed,
        })

        outcomes: list[RunOutcome] = []
        for planned in plan:
            outcome = self.execute_run(planned, timeout_s=timeout_s)
            outcomes.append(outcome)
            if stop_on_failure and not outcome.succeeded:
                self.on_event("suite.abort", {"cell": str(planned.cell)})
                break

        self.on_event("suite.done", {
            "runs": len(outcomes),
            "succeeded": sum(1 for o in outcomes if o.succeeded),
        })
        return outcomes


def flag_world_mismatches(outcomes: Sequence[RunOutcome]) -> dict[str, list[str]]:
    """Flag runs of a scenario whose world differed from the scenario's majority.

    METHODOLOGY §7 promises that runs whose fingerprints differ are never pooled.
    Enforcing it is what turns that from a claim into a property.

    The comparison is **per scenario, across variants**, not per cell. A cell
    disagreeing with itself is a broken generator; a variant disagreeing with
    the rest is the case that actually matters, because it means a mod changed
    worldgen and the frametimes being compared came from different terrain. Both
    are caught by comparing every run of a scenario against the fingerprint most
    of them share.

    Runs with no fingerprint are left alone. Absence of evidence is not evidence
    of mismatch, and flagging every run on a platform where the world could not
    be read would make the flag meaningless.

    **The run that generated the world is structurally likely to be the odd one
    out**, and it is not the mod's doing. Terrain at the edge of generation
    keeps changing while neighbouring chunks are made, so the run that stopped
    first saves a different answer from every run that restores its cache and
    lets generation finish. Measured here: on ``visual-biome-flyby`` the
    generating run disagreed with the restoring ones on 5 chunks of 1089, all
    in the outermost three rings of the region.

    It is still flagged, because a run that measured different terrain did
    measure different terrain. But it is flagged with ``world_source`` on the
    record saying which it was, so a reader is not left to conclude that a mod
    altered worldgen. ``Scenario.fingerprint_margin_gap`` is what prevents it.

    :returns: scenario id to the differing fingerprints found, for reporting.
    """
    by_scenario: dict[str, list[RunOutcome]] = {}
    for outcome in outcomes:
        if outcome.metrics is not None and outcome.world_fingerprint:
            by_scenario.setdefault(outcome.planned.cell.scenario, []).append(outcome)

    mismatches: dict[str, list[str]] = {}
    for scenario, runs in by_scenario.items():
        counts: dict[str, int] = {}
        for outcome in runs:
            counts[outcome.world_fingerprint] = (
                counts.get(outcome.world_fingerprint, 0) + 1
            )
        if len(counts) < 2:
            continue

        # A strict majority is the reference. Without one there is no reference
        # world, and every run is flagged rather than one group being blamed:
        # picking a side by hash string decided which variant was at fault by
        # something with no bearing on the question.
        best = max(counts.values())
        leaders = [digest for digest, count in counts.items() if count == best]
        reference = leaders[0] if len(leaders) == 1 and best * 2 > len(runs) else None
        for outcome in runs:
            if reference is None or outcome.world_fingerprint != reference:
                assert outcome.metrics is not None
                if RunFlag.WORLD_FINGERPRINT_MISMATCH not in outcome.metrics.flags:
                    outcome.metrics.flags.append(RunFlag.WORLD_FINGERPRINT_MISMATCH)
        mismatches[scenario] = sorted(d for d in counts if d != reference)

    return mismatches


def outcomes_to_cells(
    outcomes: Sequence[RunOutcome],
) -> dict[str, list[dict[str, Any]]]:
    """Convert run outcomes into the analysis input format.

    **Every planned attempt is serialised**, including ones that produced no
    metrics: skipping them made the document describe the runs that worked
    rather than the experiment that was run. Failed attempts carry
    ``status: "failed"`` and empty ``values``, so they stay out of every
    estimate and visible in every report.

    Keeps the execution position on every run so the report can plot order
    effects and an operator can audit whether interleaving actually held.

    World mismatches are flagged here rather than left to the caller, because
    this is the one funnel every run passes through on its way into an
    aggregate, and a fairness check that an analysis path can bypass is not a
    check.
    """
    flag_world_mismatches(outcomes)

    cells: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        key = f"{outcome.planned.cell.scenario}/{outcome.planned.cell.variant}"
        cells.setdefault(key, []).append(outcome.to_record())
    return cells


def run_counts(outcomes: Sequence[RunOutcome]) -> dict[str, dict[str, int]]:
    """Per cell: attempted, completed, admissible, and failed.

    The gaps between them are the interesting part: ``attempted`` is what the
    suite cost, ``completed`` what produced numbers, ``admissible`` what may
    enter a comparison. A gap between the first two is an unstable environment;
    between the last two, a methodology violation.
    """
    counts: dict[str, dict[str, int]] = {}
    for outcome in outcomes:
        key = str(outcome.planned.cell)
        entry = counts.setdefault(
            key, {"attempted": 0, "completed": 0, "admissible": 0, "failed": 0}
        )
        entry["attempted"] += 1
        if outcome.metrics is None:
            entry["failed"] += 1
            continue
        entry["completed"] += 1
        if outcome.metrics.admissible:
            entry["admissible"] += 1
    return counts
