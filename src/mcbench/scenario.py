"""Scenario loading, validation, and content hashing.

Implements docs/METHODOLOGY.md section 7. A scenario is a *recipe*, never a
world save: a seed plus a scripted setup sequence from which the world is
generated on the operator's machine. That is a licensing requirement (see
docs/LICENSING.md) and independently the right call, since a distributed save
silently embeds the generator version of whoever produced it.

Validation is deliberately self-contained rather than delegating to a JSON
Schema library. The schema in schemas/ remains the normative document for
external tooling, but a benchmark whose results people are asked to trust should
be verifiable without installing anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from .metrics import METRICS

__all__ = [
    "Side",
    "Category",
    "Preset",
    "ScenarioError",
    "Scenario",
    "load_scenario",
    "load_scenarios",
]

_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_VALID_OPS = {
    "wait", "teleport", "camera_path", "look", "fill", "setblock",
    "summon", "spawn_ring", "give", "command", "gamerule",
    "load_chunks", "generate_chunks", "unload_chunks", "place_structure",
    "set_render_distance", "set_simulation_distance", "loop",
}
_VALID_GENERATORS = {"default", "flat", "amplified", "large_biomes", "void"}
_VALID_REQUIREMENTS = {"carpet", "gamemode-creative", "flat-world"}


class Side(str, Enum):
    CLIENT = "client"
    SERVER = "server"
    BOTH = "both"

    @property
    def measures_frames(self) -> bool:
        """Whether a run of this scenario has frames to time at all.

        The same question ``ProbeConfig.measuresFrames()`` answers in Java, and
        it must answer it the same way. The probe and the harness disagreeing
        about whether a run has a client is the kind of seam that produces an
        empty result file and no explanation.
        """
        return self is not Side.SERVER


class Category(str, Enum):
    VISUAL = "visual"
    TICK_STABILITY = "tick-stability"
    ENTITY = "entity"
    WORLDGEN = "worldgen"
    REDSTONE = "redstone"
    LIGHTING = "lighting"
    FLUID = "fluid"
    BLOCK_ENTITY = "block-entity"
    CHUNK_IO = "chunk-io"
    MEMORY = "memory"
    REFERENCE = "reference"


class Preset(str, Enum):
    QUICK = "quick"
    FULL = "full"
    LONG = "long"


class ScenarioError(ValueError):
    """A scenario definition is invalid.

    Raised eagerly at load time. A malformed scenario discovered halfway through
    a multi-hour suite has already wasted the suite.
    """


@dataclass(frozen=True)
class Scenario:
    """A validated scenario definition."""

    id: str
    version: str
    title: str
    side: Side
    category: Category
    world: dict[str, Any]
    measurement: dict[str, Any]
    description: str = ""
    tags: tuple[str, ...] = ()
    minecraft_versions: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    setup: tuple[dict[str, Any], ...] = ()
    workload: tuple[dict[str, Any], ...] = ()
    source_path: Path | None = None
    content_hash: str = ""

    @property
    def major_version(self) -> int:
        """Results are never pooled across a change in this number."""
        match = _VERSION_PATTERN.match(self.version)
        assert match is not None  # guaranteed by validation
        return int(match.group(1))

    @property
    def seed(self) -> int:
        return int(self.world["seed"])

    @property
    def fingerprint_radius_chunks(self) -> int | None:
        """Chunks either side of spawn the world fingerprint covers.

        ``None`` when the scenario declares no region, which means every saved
        chunk. Declaring one is what keeps the hash about the terrain the
        scenario cares about rather than about how far a particular run's chunk
        loading happened to reach.
        """
        region = self.world.get("fingerprint_region")
        if not isinstance(region, dict):
            return None
        radius = region.get("radius_chunks")
        return int(radius) if isinstance(radius, int) else None

    @property
    def uses_tick_warp(self) -> bool:
        return bool(self.measurement.get("tick_warp", False))

    @property
    def saturated(self) -> bool:
        return bool(self.measurement.get("saturated", False))

    @property
    def primary_metric(self) -> str | None:
        return self.measurement.get("primary_metric")

    def worldgen_reach_gap(self) -> str:
        """Where the camera goes that setup did not pre-generate, or "".

        A client scenario pre-generates terrain so the measurement never pays
        worldgen cost. The area that has to cover is not the camera path: a
        client loads what it can see, so the reach is the furthest camera point
        plus the render distance.

        Measured on this repository's own baseline, which pre-generates 841
        chunks: the saved world came out with 3684, reaching 34 chunks from
        spawn against a pre-generated 14. Three quarters of its terrain was
        made while the scenario was running. A shared world hides most of that,
        since only the first run of a scenario generates and every later one
        restores it, but with ``--fresh-world`` every run pays, and unevenly.

        Reported rather than refused. Raising the pre-generated radius changes
        what a run costs and so is a version bump, which is the author's call
        and not something to do quietly on their behalf.
        """
        if self.side is Side.SERVER:
            return ""
        generated = next(
            (
                int(step["radius_chunks"])
                for step in self.setup
                if step.get("op") == "generate_chunks"
            ),
            None,
        )
        if generated is None:
            return ""
        render = next(
            (
                int(step["chunks"])
                for step in self.setup
                if step.get("op") == "set_render_distance"
            ),
            0,
        )
        spawn = self.world.get("spawn") or {}
        centre_x = int(spawn.get("x", 0)) // 16
        centre_z = int(spawn.get("z", 0)) // 16

        reach = 0
        for step in self.workload:
            if step.get("op") != "camera_path":
                continue
            for point in step.get("points", ()):
                distance = max(
                    abs(int(point.get("x", 0)) // 16 - centre_x),
                    abs(int(point.get("z", 0)) // 16 - centre_z),
                )
                reach = max(reach, distance + render)

        if reach <= generated:
            return ""
        return (
            f"the camera reaches {reach} chunks from spawn once render distance "
            f"{render} is counted, and setup pre-generates {generated}, so the "
            f"run generates terrain while being measured"
        )

    #: Chunks of settled terrain a fingerprint region needs inside the
    #: pre-generated one.
    #:
    #: Worldgen is not chunk-local. Features cross boundaries, so a chunk near
    #: the edge of what has been generated keeps changing while its neighbours
    #: are still being made, and two runs that stopped at different points
    #: disagree about it.
    #:
    #: Six because six is the smallest margin observed to hold, not because
    #: anything in Minecraft says six. Measured on this repository's own client
    #: scenarios: visual-biome-flyby fingerprints a radius of 16 inside a
    #: pre-generated 20, a margin of 4, and the run that made the world
    #: disagreed with the runs that restored it on 5 of 1089 chunks, every one
    #: of them in rings 14 to 16. reference-hardware-baseline fingerprints 8
    #: inside 14, a margin of 6, and thirty runs agree to the digest.
    #:
    #: A larger number would have been safer to assert and would have flagged a
    #: configuration measured to work, which is the more expensive mistake: a
    #: check that fires on something known to be fine gets turned off.
    FINGERPRINT_MARGIN_CHUNKS: ClassVar[int] = 6

    def fingerprint_margin_gap(self) -> str:
        """Whether the fingerprint region sits in settled terrain, or "".

        A fingerprint taken too close to the edge of generation is not a check
        on whether two runs measured the same world; it is a check on when each
        of them stopped generating. The run that makes the world is the one
        least settled and also the one the cache is taken from, so it is the
        run that ends up in the minority and is refused.
        """
        radius = self.fingerprint_radius_chunks
        if radius is None:
            return ""
        generated = next(
            (
                int(step["radius_chunks"])
                for step in self.setup
                if step.get("op") == "generate_chunks"
            ),
            None,
        )
        if generated is None:
            return ""
        margin = generated - radius
        if margin >= self.FINGERPRINT_MARGIN_CHUNKS:
            return ""
        return (
            f"the fingerprint covers a radius of {radius} inside a pre-generated "
            f"{generated}, leaving {margin} chunk(s) of settled terrain where "
            f"{self.FINGERPRINT_MARGIN_CHUNKS} are wanted; the outer rings keep "
            f"changing while their neighbours generate, so the run that made the "
            f"world will not match the runs that restore it"
        )

    def duration(self, preset: Preset = Preset.FULL) -> float:
        """Measurement window for a preset, falling back to ``full``.

        Falling back rather than failing keeps every scenario runnable at every
        preset, so a suite can be run quickly end-to-end before committing hours
        to it.
        """
        durations = self.measurement["duration"]
        return float(durations.get(preset.value, durations["full"]))

    @property
    def pool_key(self) -> str:
        """Identity that results must share to be pooled together.

        Combines id, major version, and content hash. Two runs whose pool keys
        differ measured different things, however similar their names.
        """
        return f"{self.id}@{self.major_version}#{self.content_hash[:12]}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioError(message)


def _validate_world(world: Any, where: str) -> None:
    _require(isinstance(world, dict), f"{where}: 'world' must be an object")
    _require("seed" in world, f"{where}: world.seed is required; an unfixed seed "
                              f"makes the comparison meaningless")
    _require(isinstance(world["seed"], int) and not isinstance(world["seed"], bool),
             f"{where}: world.seed must be an integer")
    _require("generator" in world, f"{where}: world.generator is required")
    _require(
        world["generator"] in _VALID_GENERATORS,
        f"{where}: unknown generator {world['generator']!r}; "
        f"expected one of {sorted(_VALID_GENERATORS)}",
    )

    spawn = world.get("spawn")
    if spawn is not None:
        _require(isinstance(spawn, dict), f"{where}: world.spawn must be an object")
        for axis in ("x", "y", "z"):
            _require(axis in spawn, f"{where}: world.spawn.{axis} is required")
            _require(
                isinstance(spawn[axis], (int, float)) and not isinstance(spawn[axis], bool),
                f"{where}: world.spawn.{axis} must be a number",
            )


def _validate_measurement(measurement: Any, where: str) -> None:
    _require(isinstance(measurement, dict), f"{where}: 'measurement' must be an object")

    warmup = measurement.get("warmup")
    _require(isinstance(warmup, dict), f"{where}: measurement.warmup is required")
    _require("min" in warmup, f"{where}: measurement.warmup.min is required")
    _require(
        isinstance(warmup["min"], (int, float)) and not isinstance(warmup["min"], bool)
        and warmup["min"] > 0,
        f"{where}: measurement.warmup.min must be a positive number; the JVM's "
        f"warmup is the largest confound in Minecraft benchmarking",
    )

    duration = measurement.get("duration")
    _require(isinstance(duration, dict), f"{where}: measurement.duration is required")
    _require("full" in duration, f"{where}: measurement.duration.full is required")
    for preset in ("quick", "full", "long"):
        if preset in duration:
            value = duration[preset]
            _require(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and value > 0,
                f"{where}: measurement.duration.{preset} must be a positive number",
            )

    primary = measurement.get("primary_metric")
    if primary is not None:
        _require(
            primary in METRICS,
            f"{where}: primary_metric {primary!r} is not in the metric registry; "
            f"see mcbench.metrics.METRICS",
        )
    for metric in measurement.get("metrics", []) or []:
        _require(
            metric in METRICS,
            f"{where}: metric {metric!r} is not in the metric registry",
        )


def _validate_actions(actions: Any, key: str, where: str) -> None:
    _require(isinstance(actions, list), f"{where}: '{key}' must be an array")
    for index, action in enumerate(actions):
        _require(isinstance(action, dict), f"{where}: {key}[{index}] must be an object")
        _require("op" in action, f"{where}: {key}[{index}] is missing 'op'")
        _require(
            action["op"] in _VALID_OPS,
            f"{where}: {key}[{index}] has unknown op {action['op']!r}; "
            f"expected one of {sorted(_VALID_OPS)}",
        )


def _content_hash(data: dict[str, Any]) -> str:
    """Stable hash over everything that could change the numbers.

    Excludes purely descriptive fields: retitling a scenario must not invalidate
    a corpus, but changing its seed or its workload must.
    """
    significant = {
        k: v
        for k, v in data.items()
        if k not in ("title", "description", "tags")
    }
    encoded = json.dumps(significant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate(data: Any, where: str) -> None:
    _require(isinstance(data, dict), f"{where}: scenario must be a JSON object")

    for required in ("id", "version", "title", "side", "category", "world", "measurement"):
        _require(required in data, f"{where}: missing required field {required!r}")

    _require(
        isinstance(data["id"], str) and bool(_ID_PATTERN.match(data["id"])),
        f"{where}: id must be kebab-case, got {data['id']!r}",
    )
    _require(
        isinstance(data["version"], str) and bool(_VERSION_PATTERN.match(data["version"])),
        f"{where}: version must be semantic (MAJOR.MINOR.PATCH), got {data['version']!r}",
    )
    _require(
        isinstance(data["title"], str) and data["title"].strip() != "",
        f"{where}: title must be a non-empty string",
    )

    try:
        Side(data["side"])
    except ValueError:
        raise ScenarioError(
            f"{where}: unknown side {data['side']!r}; "
            f"expected one of {[s.value for s in Side]}"
        ) from None
    try:
        Category(data["category"])
    except ValueError:
        raise ScenarioError(
            f"{where}: unknown category {data['category']!r}; "
            f"expected one of {[c.value for c in Category]}"
        ) from None

    _validate_world(data["world"], where)
    _validate_measurement(data["measurement"], where)

    for key in ("setup", "workload"):
        if key in data:
            _validate_actions(data[key], key, where)

    for requirement in data.get("requires", []) or []:
        _require(
            requirement in _VALID_REQUIREMENTS,
            f"{where}: unknown requirement {requirement!r}; "
            f"expected one of {sorted(_VALID_REQUIREMENTS)}",
        )

    # Tick warp is a server-side driver; asking for it on a client scenario is a
    # definition error, not something to quietly ignore at run time.
    measurement = data["measurement"]
    if measurement.get("tick_warp") and data["side"] == Side.CLIENT.value:
        raise ScenarioError(
            f"{where}: tick_warp is server-side but side is 'client'"
        )
    # Carpet is what provides tick warp below 1.20.3, where vanilla has no
    # /tick. Recorded on the scenario because the scenario is version-
    # independent and this is the older half of the answer; which half applies
    # to a given run is decided by Capability.TICK_WARP against its target, and
    # that is the check with teeth. `requires` is a declaration, not a gate.
    if measurement.get("tick_warp") and "carpet" not in (data.get("requires") or []):
        raise ScenarioError(
            f"{where}: tick_warp needs vanilla /tick (1.20.3+) or Carpet below "
            f"that; declare \"carpet\" in 'requires' so a target that has "
            f"neither is refused rather than measured at 20 TPS"
        )


def parse_scenario(data: dict[str, Any], *, source: Path | None = None) -> Scenario:
    """Validate a decoded scenario document and build a :class:`Scenario`."""
    where = str(source) if source else f"scenario {data.get('id', '<unknown>')!r}"
    _validate(data, where)

    return Scenario(
        id=data["id"],
        version=data["version"],
        title=data["title"],
        side=Side(data["side"]),
        category=Category(data["category"]),
        world=data["world"],
        measurement=data["measurement"],
        description=data.get("description", ""),
        tags=tuple(data.get("tags", ())),
        minecraft_versions=tuple(data.get("minecraft_versions", ())),
        requires=tuple(data.get("requires", ())),
        setup=tuple(data.get("setup", ())),
        workload=tuple(data.get("workload", ())),
        source_path=source,
        content_hash=_content_hash(data),
    )


def load_scenario(path: str | Path) -> Scenario:
    """Load and validate a single scenario file."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"{path}: invalid JSON: {exc}") from exc
    return parse_scenario(data, source=path)


def load_scenarios(root: str | Path) -> list[Scenario]:
    """Load every scenario under ``root``, rejecting duplicate ids.

    Duplicate ids are fatal rather than last-one-wins: two files claiming the
    same id means results from different definitions would silently pool.
    """
    root = Path(root)
    if not root.exists():
        raise ScenarioError(f"scenario root does not exist: {root}")

    scenarios: list[Scenario] = []
    seen: dict[str, Path] = {}
    for path in sorted(root.rglob("*.json")):
        scenario = load_scenario(path)
        if scenario.id in seen:
            raise ScenarioError(
                f"duplicate scenario id {scenario.id!r} in {path} "
                f"(already defined in {seen[scenario.id]})"
            )
        seen[scenario.id] = path
        scenarios.append(scenario)
    return scenarios


def select(
    scenarios: Iterable[Scenario],
    *,
    side: Side | None = None,
    category: Category | None = None,
    tags: Iterable[str] | None = None,
) -> list[Scenario]:
    """Filter scenarios by side, category, and/or tags."""
    wanted_tags = set(tags) if tags else None
    out = []
    for scenario in scenarios:
        if side is not None and scenario.side not in (side, Side.BOTH):
            continue
        if category is not None and scenario.category is not category:
            continue
        if wanted_tags is not None and not wanted_tags & set(scenario.tags):
            continue
        out.append(scenario)
    return out
