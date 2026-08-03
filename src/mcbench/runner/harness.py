"""The headless run loop.

Executes a :class:`~mcbench.planner.RunPlan` against real instances: provision,
launch, collect the probe stream, reduce to metrics, tear down, repeat. Designed
so a developer can get a verdict from one command and a CI job can consume the
result without parsing prose.

Instance provisioning is delegated to HeadlessMC as an external process. That
boundary is deliberate — HeadlessMC owns Mojang authentication and game
provisioning, so mcbench never touches credentials and never redistributes game
files (docs/LICENSING.md).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Platform, SuiteConfig, Variant
from ..metrics import (
    RunFlag,
    RunMetrics,
    frame_cap_suspected,
    reduce_client_run,
    reduce_server_run,
)
from ..planner import Cell, PlannedRun, RunPlan, plan_runs
from ..providers import ModrinthClient, ModrinthError, ResolvedMod
from ..scenario import Scenario, Side
from ..targets import Target
from ..world import WorldError, create_world, fingerprint_world
from .plan import check_target, write_plan
from .preflight import Check, Preflight, Severity, run_preflight
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
#: where the limiter is disabled — but it is still a number in a file, and mods
#: are free to honour it literally. Recorded in provenance, and
#: :func:`~mcbench.metrics.frame_cap_suspected` checks for runs that hit it.
CLIENT_FPS_CAP = 260



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
    #: world could not be read — never a placeholder, because a fingerprint that
    #: two runs share only because both failed to compute one would silently
    #: certify exactly what it exists to check.
    world_fingerprint: str = ""

    @property
    def succeeded(self) -> bool:
        return self.metrics is not None and self.metrics.admissible

    @property
    def status(self) -> str:
        """How this attempt ended, in one word.

        ``failed`` means no metrics at all — the launch died, timed out, or the
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

    Suite manifests are untrusted input — a public corpus or a CI job would
    benchmark suites submitted by other people — and this path is taken straight
    from one. Without confinement, ``../../../../etc/passwd`` reads an arbitrary
    host file and copies it into the instance.

    Absolute paths and traversal are both refused rather than normalised. The
    legitimate case — a developer benchmarking ``build/libs/mymod.jar`` — is a
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
    published yet — the common case during development. They are recorded with a
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
        GPU regardless of what its scenarios declare — and demanding one would
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

    def resolve_probe(self) -> ProbeArtifacts:
        """Make sure the probe — and what the probe needs — is on disk.

        Fabric API is fetched from Modrinth when not supplied, and recorded in
        provenance like any other artefact — it is installed in every instance
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
        return (
            self.work_dir / "instances"
            / f"{planned.cell.scenario}__{planned.cell.variant}__r{planned.replicate}"
        )

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
        small — which is worse than no variance estimate, because it looks
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

        if scenario.side in (Side.CLIENT, Side.BOTH):
            # Author the world quick-play enters; it must exist beforehand.
            create_world(
                instance / "saves",
                name=self._client_world_name(scenario),
                seed=scenario.seed,
                generator=str(scenario.world.get("generator", "default")),
                spawn=_spawn_of(scenario),
            )

            options = instance / "options.txt"
            # Vsync off: a vsync-locked client measures the display, not the
            # renderer. maxFps is the top of vanilla's slider, which disables
            # the limiter — written from a named constant and recorded, not
            # described as "uncapped".
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

        if str(self.headlessmc).endswith(".jar"):
            command = ["java", "-jar", str(self.headlessmc)]
        else:
            command = [str(self.headlessmc)]

        command += [
            "launch", self.suite.minecraft_version,
            "--loader", self.suite.loader.value,
            "--gamedir", str(instance),
            "--jvm", " ".join(jvm_args),
        ]
        # A pinned loader version has to be requested, or two runs on different
        # loader builds serialise as the same configuration.
        if self.suite.loader_version:
            command += ["--loader-version", str(self.suite.loader_version)]

        if scenario.side is Side.SERVER:
            command.append("--server")
        elif scenario.side in (Side.CLIENT, Side.BOTH):
            # Quick-play into the scenario's world. Without it the client sits
            # at the title screen, no integrated server starts, and the probe
            # never fires.
            command += ["--quickPlaySingleplayer", self._client_world_name(scenario)]
        return command

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
        substitutes — inventing a value would be fabricating a measurement.
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
        # the two sides cannot drift into writing and reading different files —
        # which has happened once already on this seam.
        environment["MCBENCH_AGENT_OUTPUT"] = str(agent_path)

        try:
            with log_path.open("w", encoding="utf-8") as log:
                completed = subprocess.run(
                    command, cwd=instance, env=environment,
                    stdout=log, stderr=subprocess.STDOUT,
                    timeout=timeout_s, check=False,
                )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            error = f"run exceeded its {timeout_s:.0f}s timeout"
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"failed to launch: {exc}"

        wall_clock = time.monotonic() - started

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

        metrics = self._reduce(stream, scenario)
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

        return RunOutcome(
            planned=planned, metrics=metrics, stream=stream,
            wall_clock_s=wall_clock, exit_code=exit_code, log_path=log_path,
            error=error, world_fingerprint=fingerprint,
        )

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

    def _fingerprint_world(
        self, instance: Path, scenario: Scenario, planned: PlannedRun
    ) -> str:
        """Hash the world this run measured, for the pooling check.

        METHODOLOGY §7: runs whose worlds differ are never pooled. This is what
        makes that enforceable — without it the claim is a promise the code does
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
        try:
            result = fingerprint_world(world_dir)
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
        })
        # An incomplete read is not a fingerprint. Two worlds that both failed on
        # the same region would hash identically over what was left.
        return result.sha256 if result.complete else ""

    def _adopt_agent_stream(
        self, stream: ProbeStream, agent_path: Path, planned: PlannedRun
    ) -> None:
        """Take frame timings from the JVM agent when the adapter had none.

        Absence of the file is the normal case — the agent is only present when
        an operator added ``-javaagent`` to the launch — so it is not an error.
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
    def _reduce(stream: ProbeStream, scenario: Scenario) -> RunMetrics:
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

        if scenario.side.measures_frames and stream.client.frametimes_ns:
            frames_ms = [ns / 1_000_000.0 for ns in stream.client.frametimes_ns]
            # The client sitting against its own limiter means what was measured
            # is the cap. Every variant would score it and the comparison would
            # confidently report equivalence.
            capped = (
                frame_cap_suspected(frames_ms, CLIENT_FPS_CAP)
                and RunFlag.FRAME_CAP_SUSPECTED not in metrics.flags
            )
            if capped:
                metrics.flags.append(RunFlag.FRAME_CAP_SUSPECTED)

        return metrics

    def run_suite(
        self, *, stop_on_failure: bool = False, timeout_s: float | None = None
    ) -> list[RunOutcome]:
        """Execute the whole plan in its scheduled order.

        The order is the plan's order and is never re-sorted for convenience —
        it is the fairness guarantee (planner.py). Runs that fail are recorded
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

    The comparison is **per scenario, across variants** — not per cell. A cell
    disagreeing with itself is a broken generator; a *variant* disagreeing with
    the rest is the case that actually matters, because it means a mod changed
    worldgen and the frametimes being compared came from different terrain. Both
    are caught by comparing every run of a scenario against the fingerprint most
    of them share.

    Runs with no fingerprint are left alone. Absence of evidence is not evidence
    of mismatch, and flagging every run on a platform where the world could not
    be read would make the flag meaningless.

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

        # The majority world is the reference. Ties break on the fingerprint
        # string so the choice is deterministic rather than dependent on run
        # order — an arbitrary but *stable* reference beats a shifting one.
        reference = max(sorted(counts), key=lambda digest: counts[digest])
        for outcome in runs:
            if outcome.world_fingerprint != reference:
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
