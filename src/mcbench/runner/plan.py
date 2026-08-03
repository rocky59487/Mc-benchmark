"""Compile a scenario into the execution plan the probe consumes.

The harness owns scenario interpretation; the probe just executes. This module is
that translation: scenario actions in, a flat properties file plus two command
lists out (see ``ProbeConfig`` on the Java side).

Commands are the target language because they are the most stable interface
Minecraft has. ``/fill``, ``/summon``, ``/setblock`` and ``/gamerule`` have been
compatible for a decade, while the Java methods behind them have been renamed
every few releases. Compiling to commands is what lets one scenario file run
across many game versions.

Two rules govern everything here:

- **Never silently emit nothing.** An action this module does not understand
  raises. A scenario that claims to build four hundred hopper chains and
  silently builds none would still produce numbers, and those numbers would look
  entirely valid.
- **Pace on ticks, never on frames.** Timing directives are expressed in game
  ticks, so a scripted workload advances at the same rate on a fast machine as a
  slow one. Otherwise the two configurations under comparison would be doing
  different amounts of work.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Loader
from ..scenario import Preset, Scenario, Side
from ..targets import (
    Capability,
    Dialect,
    Target,
    UnsupportedTarget,
    required_capabilities,
)

__all__ = [
    "PlanError",
    "ProbePlan",
    "compile_plan",
    "scenario_ops",
    "write_plan",
]

#: Used when a caller does not name a target. Modern enough to support every
#: capability, so an untargeted compile exercises the full command set.
DEFAULT_TARGET = Target(platform=Loader.FABRIC, minecraft_version="1.21.1",
                        mods=frozenset({"carpet"}))

#: Vanilla ``/fill`` refuses volumes larger than this, so large regions are split.
#: Exceeding it is a command failure at run time, which would leave the world in a
#: half-built state that still measures as if it were fine.
MAX_FILL_VOLUME = 32768

#: Vanilla ``/forceload add`` accepts at most this many chunks per command.
MAX_FORCELOAD_CHUNKS = 256

#: Camera paths advance one step per server tick (20 Hz).
CAMERA_STEPS_PER_SECOND = 20

VANILLA_FILL_MODES = {"replace", "destroy", "hollow", "keep", "outline"}

#: Minecraft truncates over-long commands, which would silently run a different
#: command than the scenario declared.
MAX_COMMAND_LENGTH = 32500

#: How a scenario names the player it is driving.
#:
#: Not ``@s``. The probe dispatches through the platform's command dispatcher,
#: whose source is the server rather than an entity, so ``@s`` selects nothing
#: and the game rejects the command with "No entity was found". A camera path
#: written that way emits one rejected teleport per frame and the client never
#: moves: the run completes, reports frametimes, and measures a stationary
#: camera at spawn instead of the flyby the scenario describes.
#:
#: A benchmark instance has exactly one player, so nearest-player is that player
#: from any source.
PLAYER = "@p"


#: Characters that must never appear inside an emitted command.
#:
#: The plan is a newline-delimited file, so a command containing a newline
#: becomes several commands when written, one scenario action silently
#: turning into three. Scenarios are meant to be shared and committed, which
#: makes them untrusted input, so this is a real injection vector rather than a
#: theoretical one. Carriage returns and NULs are rejected for the same reason.
_FORBIDDEN_IN_COMMAND = re.compile(r"[\x00-\x1f\x7f]")


class PlanError(ValueError):
    """A scenario action could not be compiled."""


def _check_command(line: str, where: str) -> str:
    """Reject a command that would break out of its line.

    Refuses rather than escapes or strips. A scenario doing this is either
    malicious or broken, and silently rewriting it into something that *looks*
    valid would hide both cases.
    """
    match = _FORBIDDEN_IN_COMMAND.search(line)
    if match:
        char = match.group()
        raise PlanError(
            f"{where}: command contains a forbidden control character "
            f"{char!r} (0x{ord(char):02x}). The plan is newline-delimited, so "
            f"this would inject additional commands into the run."
        )
    if len(line) > MAX_COMMAND_LENGTH:
        # Minecraft's own command buffer is bounded; an over-long line is
        # truncated at runtime, which silently executes a *different* command.
        raise PlanError(
            f"{where}: command is {len(line)} characters, over the "
            f"{MAX_COMMAND_LENGTH} limit; it would be truncated at runtime"
        )
    return line


@dataclass
class ProbePlan:
    """Everything the probe needs for one run."""

    properties: dict[str, str] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    workload: list[str] = field(default_factory=list)
    #: Settings that are not commands, such as render distance and simulation
    #: distance, which the harness applies to the instance's config files.
    instance_settings: dict[str, Any] = field(default_factory=dict)

    def write(self, directory: str | Path) -> Path:
        """Write the plan; returns the properties path the probe is pointed at."""
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)

        properties_path = root / "probe.properties"
        properties_path.write_text(
            "# Generated by mcbench. Do not edit; regenerate from the scenario.\n"
            + "".join(f"{k}={v}\n" for k, v in sorted(self.properties.items())),
            encoding="utf-8",
        )
        (root / "setup.txt").write_text(_render(self.setup), encoding="utf-8")
        (root / "workload.txt").write_text(_render(self.workload), encoding="utf-8")
        return properties_path


def _render(lines: Sequence[str]) -> str:
    header = "# Generated by mcbench. One entry per line; '@wait N' pauses N ticks.\n"
    return header + "".join(f"{line}\n" for line in lines)


# --------------------------------------------------------------------------
# Coordinate helpers
# --------------------------------------------------------------------------


def _num(value: Any) -> str:
    """Format a coordinate, keeping integers integral.

    ``/setblock 1.0 2.0 3.0`` is a syntax error, so this cannot just use str().
    """
    if isinstance(value, bool):
        raise PlanError(f"expected a number, got boolean {value!r}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    raise PlanError(f"expected a number, got {value!r}")


def _point(raw: Any, where: str) -> tuple[float, float, float]:
    if not isinstance(raw, dict):
        raise PlanError(f"{where}: expected a point object, got {raw!r}")
    try:
        return float(raw["x"]), float(raw["y"]), float(raw["z"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"{where}: invalid point {raw!r}: {exc}") from exc


def _split_volume(
    start: tuple[int, int, int], end: tuple[int, int, int]
) -> list[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    """Split a box into sub-boxes each within the vanilla fill limit.

    Splitting along Y first keeps each piece a wide flat slab, which matches how
    scenario regions are usually shaped and keeps the piece count low.
    """
    x0, y0, z0 = (min(a, b) for a, b in zip(start, end, strict=True))
    x1, y1, z1 = (max(a, b) for a, b in zip(start, end, strict=True))

    width, height, depth = x1 - x0 + 1, y1 - y0 + 1, z1 - z0 + 1
    if width * height * depth <= MAX_FILL_VOLUME:
        return [((x0, y0, z0), (x1, y1, z1))]

    layer = width * depth
    if layer > MAX_FILL_VOLUME:
        # Even a single Y layer is too large; split that layer along Z as well.
        rows_per_piece = max(1, MAX_FILL_VOLUME // max(1, width))
        pieces = []
        for y in range(y0, y1 + 1):
            for z in range(z0, z1 + 1, rows_per_piece):
                z_end = min(z + rows_per_piece - 1, z1)
                pieces.append(((x0, y, z), (x1, y, z_end)))
        return pieces

    layers_per_piece = max(1, MAX_FILL_VOLUME // layer)
    return [
        ((x0, y, z0), (x1, min(y + layers_per_piece - 1, y1), z1))
        for y in range(y0, y1 + 1, layers_per_piece)
    ]


# --------------------------------------------------------------------------
# Structure templates
# --------------------------------------------------------------------------


def _observer_clock_bank(
    origin: tuple[int, int, int], parameters: dict[str, Any], dialect: Dialect
) -> list[str]:
    """A self-sustaining observer clock driving a bank of pistons."""
    piston_count = int(parameters.get("piston_count", 4))
    x, y, z = origin
    lines = [
        f"setblock {x} {y} {z} minecraft:observer[facing=east]",
        f"setblock {x + 1} {y} {z} minecraft:observer[facing=west]",
    ]
    for i in range(piston_count):
        lines.append(f"setblock {x + 2 + i} {y} {z} minecraft:piston[facing=up]")
        lines.append(f"setblock {x + 2 + i} {y} {z + 1} minecraft:redstone_wire")
    return lines


def _hopper_chain(
    origin: tuple[int, int, int], parameters: dict[str, Any], dialect: Dialect
) -> list[str]:
    """A chain of hoppers moving items, optionally read by a comparator."""
    length = int(parameters.get("length", 16))
    x, y, z = origin
    lines = [f"setblock {x} {y} {z} minecraft:chest"]
    for i in range(1, length):
        lines.append(f"setblock {x + i} {y} {z} minecraft:hopper[facing=west]")
    if parameters.get("with_comparator"):
        lines.append(f"setblock {x + length} {y} {z} minecraft:comparator[facing=west]")
    if (preload := int(parameters.get("preload_items", 0))) > 0:
        # Pre-loaded so the measurement window sees steady-state transfer rather
        # than a fill-up transient that decays partway through.
        # Spelled by the dialect: this was /replaceitem before 1.17, and the
        # hard-coded modern form silently failed on older targets.
        lines.append(
            dialect.item_replace_block(
                x, y, z, "container.0", "minecraft:cobblestone", min(preload, 64)
            )
        )
    return lines


STRUCTURE_TEMPLATES = {
    "observer_clock_bank": _observer_clock_bank,
    "hopper_chain": _hopper_chain,
}


# --------------------------------------------------------------------------
# Action compilation
# --------------------------------------------------------------------------


def _compile_action(
    action: dict[str, Any], where: str, dialect: Dialect | None = None
) -> tuple[list[str], dict[str, Any]]:
    """Compile one action into command lines plus any instance settings."""
    if dialect is None:
        dialect = Dialect(DEFAULT_TARGET)
    op = action.get("op")
    lines: list[str] = []
    settings: dict[str, Any] = {}

    if op == "command":
        value = action.get("value")
        if not isinstance(value, str) or not value.strip():
            raise PlanError(f"{where}: 'command' needs a non-empty 'value'")
        lines.append(_check_command(value.strip().lstrip("/"), where))

    elif op == "wait":
        ticks = int(action.get("ticks", 0))
        if ticks > 0:
            lines.append(f"@wait {ticks}")

    elif op == "gamerule":
        name, value = action.get("name"), action.get("value")
        if name is None or value is None:
            raise PlanError(f"{where}: 'gamerule' needs 'name' and 'value'")
        lines.append(f"gamerule {name} {_bool_or_num(value)}")

    elif op == "setblock":
        x, y, z = _point(action.get("at") or action, f"{where}.setblock")
        block = _require_block(action, where)
        lines.append(f"setblock {_num(x)} {_num(y)} {_num(z)} {block}")

    elif op == "fill":
        lines.extend(_compile_fill(action, where))

    elif op == "summon":
        x, y, z = _point(action.get("at") or action, f"{where}.summon")
        entity = action.get("type") or action.get("entity")
        if not entity:
            raise PlanError(f"{where}: 'summon' needs 'type'")
        lines.append(f"summon {entity} {_num(x)} {_num(y)} {_num(z)}")

    elif op == "spawn_ring":
        lines.extend(_compile_spawn_ring(action, where))

    elif op == "teleport":
        x, y, z = _point(action.get("to") or action, f"{where}.teleport")
        target = action.get("target", PLAYER)
        yaw, pitch = action.get("yaw"), action.get("pitch")
        facing = f" {_num(yaw)} {_num(pitch)}" if yaw is not None and pitch is not None else ""
        lines.append(f"tp {target} {_num(x)} {_num(y)} {_num(z)}{facing}")

    elif op == "look":
        yaw = action.get("yaw", 0.0)
        pitch = action.get("pitch", 0.0)
        # Wrapped in `execute at`, because `~ ~ ~` is relative to the command
        # source. Dispatched from the server that is the world origin, so a bare
        # tp would turn the player's head by moving them there.
        lines.append(
            f"execute at {PLAYER} run tp {PLAYER} ~ ~ ~ "
            f"{_num(yaw)} {_num(pitch)}"
        )

    elif op == "give":
        item = action.get("item")
        if not item:
            raise PlanError(f"{where}: 'give' needs 'item'")
        lines.append(f"give {PLAYER} {item} {int(action.get('count', 1))}")

    elif op == "camera_path":
        lines.extend(_compile_camera_path(action, where))

    elif op in ("generate_chunks", "load_chunks"):
        lines.extend(_compile_forceload(action, where))

    elif op == "unload_chunks":
        lines.append("forceload remove all")

    elif op == "place_structure":
        lines.extend(_compile_structures(action, where, dialect))

    elif op == "set_render_distance":
        settings["render_distance"] = int(action.get("chunks", 12))

    elif op == "set_simulation_distance":
        settings["simulation_distance"] = int(action.get("chunks", 8))

    elif op == "loop":
        # The workload list already cycles, so a loop compiles to its body. A
        # tick_period spaces repetitions apart.
        body = action.get("body") or []
        for index, inner in enumerate(body):
            inner_lines, inner_settings = _compile_action(
                inner, f"{where}.loop[{index}]", dialect
            )
            lines.extend(inner_lines)
            settings.update(inner_settings)
        if (period := int(action.get("tick_period", 0))) > 0:
            lines.append(f"@wait {period}")

    else:
        # Deliberately fatal. Silently skipping would produce a world that does
        # not match the scenario, and numbers measured against it would look
        # perfectly valid.
        raise PlanError(f"{where}: no compiler for action op {op!r}")

    return lines, settings


def _bool_or_num(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _require_block(action: dict[str, Any], where: str) -> str:
    block = action.get("block")
    if not block:
        raise PlanError(f"{where}: needs 'block'")
    return str(block)


def _compile_fill(action: dict[str, Any], where: str) -> list[str]:
    start = _point(action.get("from"), f"{where}.fill.from")
    end = _point(action.get("to"), f"{where}.fill.to")
    block = _require_block(action, where)
    mode = action.get("mode", "")
    spacing = int(action.get("spacing", 0))

    if mode == "hollow_grid":
        # Not a vanilla mode: a lattice of individual blocks at a fixed spacing,
        # used to create points of interest without filling the whole volume.
        if spacing < 1:
            raise PlanError(f"{where}: 'hollow_grid' needs a positive 'spacing'")
        lines = []
        for x in range(int(start[0]), int(end[0]) + 1, spacing):
            for y in range(int(start[1]), int(end[1]) + 1, max(1, spacing)):
                for z in range(int(start[2]), int(end[2]) + 1, spacing):
                    lines.append(f"setblock {x} {y} {z} {block}")
        return lines

    if mode and mode not in VANILLA_FILL_MODES:
        raise PlanError(
            f"{where}: unknown fill mode {mode!r}; "
            f"expected one of {sorted(VANILLA_FILL_MODES)} or 'hollow_grid'"
        )

    suffix = f" {mode}" if mode else ""
    pieces = _split_volume(
        (int(start[0]), int(start[1]), int(start[2])),
        (int(end[0]), int(end[1]), int(end[2])),
    )
    return [
        f"fill {a[0]} {a[1]} {a[2]} {b[0]} {b[1]} {b[2]} {block}{suffix}"
        for a, b in pieces
    ]


def _compile_spawn_ring(action: dict[str, Any], where: str) -> list[str]:
    """Place entities on a deterministic lattice.

    A lattice rather than random placement: natural or random spawning would vary
    the population between runs and destroy comparability, which is why the
    scenarios that use this also disable ``doMobSpawning``.
    """
    entities = action.get("entities") or []
    if not entities:
        raise PlanError(f"{where}: 'spawn_ring' needs 'entities'")

    radius = float(action.get("radius", 32))
    y = float(action.get("y", 5))
    persistent = bool(action.get("persistent", False))
    no_ai = bool(action.get("no_ai", False))

    nbt_parts = []
    if persistent:
        nbt_parts.append("PersistenceRequired:1b")
    if no_ai:
        nbt_parts.append("NoAI:1b")
    nbt = "{" + ",".join(nbt_parts) + "}" if nbt_parts else ""

    lines: list[str] = []
    for entry in entities:
        entity_type = entry.get("type")
        count = int(entry.get("count", 0))
        if not entity_type or count <= 0:
            raise PlanError(f"{where}: each entity needs 'type' and a positive 'count'")

        # Square lattice covering the disc, so spacing is identical every run.
        side = max(1, math.ceil(math.sqrt(count)))
        step = (2 * radius) / side if side > 1 else 0.0
        placed = 0
        for row in range(side):
            for column in range(side):
                if placed >= count:
                    break
                x = -radius + column * step + step / 2
                z = -radius + row * step + step / 2
                # A space before the tag. Block and item NBT attaches with no
                # separator — `setblock x y z minecraft:chest{Items:[...]}` —
                # because there it is part of one argument's grammar. /summon
                # takes the tag as an argument of its own, and Brigadier
                # requires whitespace between arguments, so writing it the
                # block way ends the position and then finds `{` where a
                # separator has to be.
                lines.append(
                    f"summon {entity_type} {x:.1f} {_num(y)} {z:.1f} {nbt}".rstrip()
                )
                placed += 1
    return lines


def _compile_camera_path(action: dict[str, Any], where: str) -> list[str]:
    """Interpolate a camera path into tick-paced teleports.

    Paced in ticks rather than frames on purpose. One step per rendered frame
    would make a fast machine fly the path faster, so the two configurations
    being compared would traverse different amounts of world. The workload
    itself would differ, which is the one thing a benchmark must not allow.
    """
    points = action.get("points") or []
    if len(points) < 2:
        raise PlanError(f"{where}: 'camera_path' needs at least two points")

    duration = float(action.get("duration_s", 60))
    steps = max(2, int(duration * CAMERA_STEPS_PER_SECOND))

    parsed = []
    for index, raw in enumerate(points):
        x, y, z = _point(raw, f"{where}.points[{index}]")
        parsed.append((x, y, z, float(raw.get("yaw", 0)), float(raw.get("pitch", 0))))

    lines: list[str] = []
    segments = len(parsed) - 1
    for step in range(steps):
        position = (step / (steps - 1)) * segments
        index = min(int(position), segments - 1)
        t = position - index
        a, b = parsed[index], parsed[index + 1]
        # Linear interpolation. Catmull-Rom would be smoother, but it can
        # overshoot outside the declared points and put the camera inside terrain,
        # which changes the workload in a way the scenario author did not intend.
        x, y, z, yaw, pitch = (a[i] + (b[i] - a[i]) * t for i in range(5))
        lines.append(f"tp {PLAYER} {x:.2f} {y:.2f} {z:.2f} {yaw:.1f} {pitch:.1f}")
        lines.append("@wait 1")
    return lines


def _compile_forceload(action: dict[str, Any], where: str) -> list[str]:
    """Force-load a square chunk region, respecting the per-command chunk cap."""
    radius = int(action.get("radius_chunks", 8))
    origin = action.get("origin") or {"x": 0, "z": 0}
    cx, cz = int(origin.get("x", 0)) // 16, int(origin.get("z", 0)) // 16

    # /forceload takes block coordinates and caps at 256 chunks per command, so
    # the region is tiled into squares within that cap.
    side = max(1, int(math.isqrt(MAX_FORCELOAD_CHUNKS)))
    lines: list[str] = []
    for x0 in range(cx - radius, cx + radius + 1, side):
        for z0 in range(cz - radius, cz + radius + 1, side):
            x1 = min(x0 + side - 1, cx + radius)
            z1 = min(z0 + side - 1, cz + radius)
            lines.append(f"forceload add {x0 * 16} {z0 * 16} {x1 * 16 + 15} {z1 * 16 + 15}")
    return lines


def _compile_structures(
    action: dict[str, Any], where: str, dialect: Dialect
) -> list[str]:
    template = action.get("template")
    builder = STRUCTURE_TEMPLATES.get(template)
    if builder is None:
        raise PlanError(
            f"{where}: unknown structure template {template!r}; "
            f"expected one of {sorted(STRUCTURE_TEMPLATES)}"
        )

    count = int(action.get("count", 1))
    spacing = int(action.get("spacing", 4))
    origin = _point(action.get("origin"), f"{where}.origin")
    parameters = action.get("parameters") or {}

    side = max(1, math.ceil(math.sqrt(count)))
    lines: list[str] = []
    placed = 0
    for row in range(side):
        for column in range(side):
            if placed >= count:
                break
            at = (
                int(origin[0]) + column * spacing,
                int(origin[1]),
                int(origin[2]) + row * spacing,
            )
            lines.extend(builder(at, parameters, dialect))
            placed += 1
    return lines


# --------------------------------------------------------------------------
# Plan assembly
# --------------------------------------------------------------------------


def _compile_actions(
    actions: Iterable[dict[str, Any]], label: str, dialect: Dialect
) -> tuple[list[str], dict[str, Any]]:
    lines: list[str] = []
    settings: dict[str, Any] = {}
    for index, action in enumerate(actions):
        action_lines, action_settings = _compile_action(
            action, f"{label}[{index}]", dialect
        )
        lines.extend(action_lines)
        settings.update(action_settings)
    return lines, settings


def scenario_ops(scenario: Scenario) -> set[str]:
    """Every action op a scenario uses, including inside loop bodies.

    Capabilities are derived from this rather than from a hand-written
    ``requires`` list, so a scenario cannot forget to declare a requirement and
    then appear to run on a target that silently drops half of it.
    """
    ops: set[str] = set()

    def walk(actions: Iterable[dict[str, Any]]) -> None:
        for action in actions:
            op = action.get("op")
            if op:
                ops.add(op)
            if op == "loop":
                walk(action.get("body") or [])

    walk(scenario.setup)
    walk(scenario.workload)
    return ops


def check_target(scenario: Scenario, target: Target) -> list[Capability]:
    """Capabilities ``scenario`` needs that ``target`` lacks.

    Empty means the scenario compiles for that target. This is the check that
    makes one scenario definition safe to point at many platforms: without it a
    rendering scenario would compile happily for a headless server and record
    nothing, and a tick-warp scenario would compile for a target with no warp
    command and silently measure at 20 TPS instead.
    """
    dialect = Dialect(target)
    needed = required_capabilities(
        side=scenario.side.value,
        uses_tick_warp=scenario.uses_tick_warp,
        ops=scenario_ops(scenario),
    )
    return dialect.missing(sorted(needed, key=lambda c: c.value))


def compile_plan(
    scenario: Scenario,
    *,
    target: Target | None = None,
    preset: Preset = Preset.FULL,
    probe_output: str = "mcbench/probe.jsonl",
    strict: bool = True,
) -> ProbePlan:
    """Compile a scenario into an executable probe plan for one target.

    Args:
        target: Platform and version to compile for. Defaults to a modern Fabric
            target supporting every capability.
        strict: Raise :class:`UnsupportedTarget` when the target cannot express
            the scenario. Disabling this is for tooling that wants to report a
            compatibility matrix, never for producing a plan to actually run.
    """
    target = target or DEFAULT_TARGET
    dialect = Dialect(target)

    if strict:
        missing = check_target(scenario, target)
        if missing:
            reasons = "\n  ".join(dialect.explain(c) for c in missing)
            raise UnsupportedTarget(
                f"scenario {scenario.id!r} cannot run on {target}:\n  {reasons}"
            )

    plan = ProbePlan()

    setup_lines, setup_settings = _compile_actions(scenario.setup, "setup", dialect)
    workload_lines, workload_settings = _compile_actions(
        scenario.workload, "workload", dialect
    )

    # World-level gamerules run before anything else so setup never races the
    # rules that are meant to hold it steady, mob spawning in particular.
    gamerules = [
        f"gamerule {name} {_bool_or_num(value)}"
        for name, value in sorted((scenario.world.get("gamerules") or {}).items())
    ]
    if (time := scenario.world.get("time")) is not None:
        gamerules.append(dialect.time_set(int(time)))
    weather = scenario.world.get("weather")
    if weather:
        gamerules.append(dialect.weather(weather))
    difficulty = scenario.world.get("difficulty")
    if difficulty:
        gamerules.append(dialect.difficulty(difficulty))

    # Every emitted line is checked, not just the pass-through command op:
    # structure templates, dialect spellings and interpolated coordinates all
    # reach the same file, and a single unchecked path defeats the check.
    warmup = scenario.measurement["warmup"]
    if scenario.uses_tick_warp:
        # Last, after the world is built. The setup above contains waits counted
        # in ticks, and running those compressed would give the world less real
        # time to settle than the scenario asked for.
        #
        # Long enough to cover everything that follows. Warmup may take up to
        # its maximum before the gate opens and the measurement window follows
        # it; a warp that ran out partway would drop the server back to 20 TPS
        # mid-measurement, which is precisely the silent failure the whole
        # mechanism exists to avoid. Overshooting costs nothing: the run ends by
        # exiting the game.
        span = int(
            warmup["min"] * warmup.get("max_multiple", 3)
            + scenario.duration(preset)
        )
        setup_lines.append(dialect.tick_warp(span * 2))

    plan.setup = [
        _check_command(line, "setup") for line in gamerules + setup_lines
    ]
    plan.workload = [_check_command(line, "workload") for line in workload_lines]
    plan.instance_settings = {**setup_settings, **workload_settings}

    plan.properties = {
        "scenario.id": scenario.id,
        "scenario.version": scenario.version,
        "scenario.content_hash": scenario.content_hash,
        "scenario.side": scenario.side.value,
        "target.platform": target.platform.value,
        "target.minecraft_version": target.minecraft_version,
        "warmup.min": str(warmup["min"]),
        "warmup.max_multiple": str(warmup.get("max_multiple", 3)),
        "warmup.steady_state_tolerance": str(warmup.get("steady_state_tolerance", 0.05)),
        # Server scenarios advance in ticks and client scenarios in seconds, so a
        # window sized for one is badly wrong for the other.
        "warmup.steady_state_window": "200" if scenario.side is Side.SERVER else "120",
        "measurement.duration": str(scenario.duration(preset)),
        "measurement.tick_warp": "true" if scenario.uses_tick_warp else "false",
        "measurement.saturated": "true" if scenario.saturated else "false",
        "output": probe_output,
    }
    return plan


def write_plan(
    scenario: Scenario,
    directory: str | Path,
    *,
    target: Target | None = None,
    preset: Preset = Preset.FULL,
    probe_output: str = "mcbench/probe.jsonl",
) -> tuple[Path, ProbePlan]:
    """Compile and write a plan. Returns the properties path and the plan."""
    plan = compile_plan(
        scenario, target=target, preset=preset, probe_output=probe_output
    )
    return plan.write(directory), plan
