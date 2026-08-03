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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import Platform, SuiteConfig, Variant
from ..metrics import RunFlag, RunMetrics, reduce_client_run, reduce_server_run
from ..planner import PlannedRun, RunPlan, plan_runs
from ..providers import ModrinthClient, ModrinthError, ResolvedMod
from ..scenario import Preset, Scenario, Side
from ..targets import Target, UnsupportedTarget
from .plan import check_target, write_plan
from .preflight import Check, Preflight, Severity, run_preflight
from .protocol import ProbeError, ProbeStream, parse_probe_stream

__all__ = [
    "RunOutcome",
    "HarnessError",
    "Harness",
    "resolve_variant_mods",
]


class HarnessError(RuntimeError):
    """The harness could not execute a run or a suite."""


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

    @property
    def succeeded(self) -> bool:
        return self.metrics is not None and self.metrics.admissible


@dataclass
class ResolvedVariant:
    """A variant with every mod resolved to a concrete file on disk."""

    variant: Variant
    jars: list[Path] = field(default_factory=list)
    provenance: list[dict[str, str]] = field(default_factory=list)


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
            candidate = Path(mod.project)
            if not candidate.is_absolute() and local_root is not None:
                candidate = local_root / mod.project
            if not candidate.exists():
                raise HarnessError(
                    f"{variant.name}: local mod not found: {candidate}"
                )
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
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.suite = suite
        self.scenarios = scenarios
        self.work_dir = Path(work_dir)
        self.headlessmc = Path(headlessmc) if headlessmc else self._find_headlessmc()
        self.modrinth = modrinth or ModrinthClient(
            cache_dir=self.work_dir / "cache" / "mods"
        )
        self.local_root = Path(local_root) if local_root else Path.cwd()
        self.on_event = on_event or (lambda event, payload: None)
        self._resolved: dict[str, ResolvedVariant] = {}

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
        return result

    def resolve_all(self) -> dict[str, ResolvedVariant]:
        """Resolve every variant's mods up front.

        Done before the first launch on purpose: discovering a broken mod pin
        two hours into a suite wastes the whole suite, and the failure has
        nothing to do with the measurement.
        """
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

    def _prepare_instance(self, planned: PlannedRun, scenario: Scenario) -> Path:
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
        mods_dir = instance / "mods"
        mods_dir.mkdir(parents=True, exist_ok=True)

        resolved = self._resolved.get(planned.cell.variant)
        if resolved is None:
            raise HarnessError(
                f"variant {planned.cell.variant!r} was not resolved; "
                f"call resolve_all() first"
            )
        for jar in resolved.jars:
            shutil.copy2(jar, mods_dir / jar.name)

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

        # Render and simulation distance are not commands in vanilla, so they are
        # applied as instance configuration rather than silently dropped.
        self._apply_instance_settings(instance, scenario, plan.instance_settings)

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

    def _apply_instance_settings(
        self, instance: Path, scenario: Scenario, settings: dict[str, Any]
    ) -> None:
        """Write settings that have no command equivalent into the instance's config."""
        if not settings:
            return

        if scenario.side in (Side.SERVER, Side.BOTH):
            simulation = settings.get("simulation_distance")
            view = settings.get("render_distance")
            lines = [
                f"level-seed={scenario.seed}",
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
            render = settings.get("render_distance")
            if render is None:
                return
            options = instance / "options.txt"
            # Frame rate is left uncapped and vsync off: a capped or vsync-locked
            # client measures the display, not the renderer, so every variant
            # would score the same refresh rate and the benchmark would report
            # nothing at all.
            options.write_text(
                "\n".join(
                    [
                        f"renderDistance:{render}",
                        "maxFps:260",
                        "enableVsync:false",
                        "graphicsMode:1",
                        "gamma:1.0",
                        "pauseOnLostFocus:false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

    def _launch_command(self, instance: Path, scenario: Scenario) -> list[str]:
        if self.headlessmc is None:
            raise HarnessError(
                "HeadlessMC was not found. Install it and pass --headlessmc, or "
                "put 'headlessmc' on PATH. mcbench delegates game provisioning "
                "and Mojang authentication to HeadlessMC and never handles "
                "credentials or redistributes game files itself."
            )

        heap = f"-Xmx{self.suite.heap_mb}m"
        jvm_args = [heap, "-XX:+UseG1GC", *self.suite.baseline_variant.jvm_args]

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
        if scenario.side is Side.SERVER:
            command.append("--server")
        return command

    def execute_run(self, planned: PlannedRun, *, timeout_s: float | None = None) -> RunOutcome:
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

        instance = self._prepare_instance(planned, scenario)
        log_path = instance / "mcbench" / "instance.log"
        probe_path = instance / "mcbench" / "probe.jsonl"

        if timeout_s is None:
            # Generous headroom over the scenario's own budget: provisioning,
            # worldgen, and JVM startup are untimed but real, and killing a run
            # that was merely slow to start would bias against heavier mods.
            timeout_s = scenario.duration(self.suite.preset) * 4 + 900

        started = time.monotonic()
        exit_code: int | None = None
        error = ""

        try:
            command = self._launch_command(instance, scenario)
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

        metrics = self._reduce(stream, scenario)
        self.on_event("run.done", {
            "cell": str(planned.cell),
            "wall_clock_s": round(wall_clock, 1),
            "admissible": metrics.admissible,
            "flags": [f.value for f in metrics.flags],
        })

        return RunOutcome(
            planned=planned, metrics=metrics, stream=stream,
            wall_clock_s=wall_clock, exit_code=exit_code, log_path=log_path,
            error=error,
        )

    @staticmethod
    def _reduce(stream: ProbeStream, scenario: Scenario) -> RunMetrics:
        """Reduce a probe stream to metrics, merging client and server samples."""
        stream.server.saturated = stream.server.saturated or scenario.saturated

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


def outcomes_to_cells(
    outcomes: Sequence[RunOutcome],
) -> dict[str, list[dict[str, Any]]]:
    """Convert run outcomes into the analysis input format.

    Keeps the execution position on every run so the report can plot order
    effects and an operator can audit whether interleaving actually held.
    """
    cells: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        if outcome.metrics is None:
            continue
        key = f"{outcome.planned.cell.scenario}/{outcome.planned.cell.variant}"
        cells.setdefault(key, []).append({
            "values": outcome.metrics.values,
            "flags": [f.value for f in outcome.metrics.flags],
            "position": outcome.planned.position,
        })
    return cells
