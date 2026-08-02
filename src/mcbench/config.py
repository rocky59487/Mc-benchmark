"""Suite configuration: what to measure, against what, how many times.

A suite manifest lists mods by identifier only. It never references a jar file.
That is a licensing constraint (docs/LICENSING.md: mod jars may not be
redistributed) which happens to produce the right artefact anyway — a small
text file that can be committed, diffed, reviewed, and reproduced by anyone,
while the binaries stay on the operator's own machine.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .planner import DEFAULT_RUNS_PER_CELL, MIN_RUNS_PER_CELL, OrderStrategy
from .scenario import Preset
from .stats import DEFAULT_ROPE

__all__ = [
    "Loader",
    "Platform",
    "ModRef",
    "Variant",
    "SuiteConfig",
    "ConfigError",
    "load_suite",
]


class ConfigError(ValueError):
    """A suite manifest is invalid."""


class Loader(str, Enum):
    FABRIC = "fabric"
    NEOFORGE = "neoforge"
    FORGE = "forge"
    QUILT = "quilt"


class Platform(str, Enum):
    """Where a mod is resolved from.

    Modrinth is the default; see docs/LICENSING.md for why CurseForge is opt-in
    and requires an operator-supplied key.
    """

    MODRINTH = "modrinth"
    CURSEFORGE = "curseforge"
    LOCAL = "local"


@dataclass(frozen=True)
class ModRef:
    """A mod identified by coordinates, never by file.

    ``version`` should be pinned for any published result. A floating version
    means the manifest describes different software over time, and results
    gathered under it cannot be pooled or reproduced.
    """

    platform: Platform
    project: str
    version: str | None = None

    @property
    def pinned(self) -> bool:
        return self.version is not None

    def __str__(self) -> str:
        return f"{self.platform.value}:{self.project}" + (
            f"@{self.version}" if self.version else "@latest"
        )


@dataclass(frozen=True)
class Variant:
    """One configuration under test — a named set of mods.

    The empty mod set is the baseline. Every suite has exactly one baseline, and
    comparisons are always made against it.
    """

    name: str
    mods: tuple[ModRef, ...] = ()
    jvm_args: tuple[str, ...] = ()
    game_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def is_baseline(self) -> bool:
        return not self.mods


@dataclass(frozen=True)
class SuiteConfig:
    """A complete, validated suite definition."""

    name: str
    minecraft_version: str
    loader: Loader
    loader_version: str | None
    variants: tuple[Variant, ...]
    scenarios: tuple[str, ...]
    baseline: str
    runs_per_cell: int = DEFAULT_RUNS_PER_CELL
    preset: Preset = Preset.FULL
    order: OrderStrategy = OrderStrategy.INTERLEAVED
    rope: float = DEFAULT_ROPE
    seed: int = 0
    interactions: tuple[tuple[str, ...], ...] = ()
    heap_mb: int = 4096
    source_path: Path | None = None

    @property
    def baseline_variant(self) -> Variant:
        for variant in self.variants:
            if variant.name == self.baseline:
                return variant
        raise ConfigError(f"baseline variant {self.baseline!r} not found")

    @property
    def publishable(self) -> bool:
        """Whether results from this suite may enter the public corpus.

        Three conditions, each from METHODOLOGY.md: interleaved ordering,
        sufficient repetition, and fully pinned mod versions. A suite that fails
        any of these can still be run — it is useful for local iteration — but
        its numbers are not comparable to anyone else's.
        """
        return (
            self.order is OrderStrategy.INTERLEAVED
            and self.runs_per_cell >= MIN_RUNS_PER_CELL
            and all(mod.pinned for v in self.variants for mod in v.mods)
        )

    def unpublishable_reasons(self) -> list[str]:
        """Why :attr:`publishable` is False, for actionable reporting."""
        reasons = []
        if self.order is not OrderStrategy.INTERLEAVED:
            reasons.append(
                f"execution order is {self.order.value!r}; only 'interleaved' "
                f"controls for thermal and background drift"
            )
        if self.runs_per_cell < MIN_RUNS_PER_CELL:
            reasons.append(
                f"runs_per_cell is {self.runs_per_cell}; at least "
                f"{MIN_RUNS_PER_CELL} are needed to estimate variance"
            )
        floating = [
            f"{v.name}/{m.project}"
            for v in self.variants
            for m in v.mods
            if not m.pinned
        ]
        if floating:
            reasons.append(
                "unpinned mod versions make the suite unreproducible: "
                + ", ".join(sorted(floating))
            )
        return reasons


def _parse_mod(entry: Any, where: str) -> ModRef:
    """Accept either ``"modrinth:sodium@mc1.21-0.6.0"`` or a table."""
    if isinstance(entry, str):
        platform_part, _, rest = entry.partition(":")
        if not rest:
            raise ConfigError(
                f"{where}: mod {entry!r} must be 'platform:project[@version]'"
            )
        project, _, version = rest.partition("@")
        try:
            platform = Platform(platform_part)
        except ValueError:
            raise ConfigError(
                f"{where}: unknown platform {platform_part!r}; "
                f"expected one of {[p.value for p in Platform]}"
            ) from None
        return ModRef(platform=platform, project=project, version=version or None)

    if isinstance(entry, dict):
        if "project" not in entry:
            raise ConfigError(f"{where}: mod entry is missing 'project'")
        try:
            platform = Platform(entry.get("platform", "modrinth"))
        except ValueError:
            raise ConfigError(
                f"{where}: unknown platform {entry.get('platform')!r}"
            ) from None
        return ModRef(
            platform=platform,
            project=entry["project"],
            version=entry.get("version"),
        )

    raise ConfigError(f"{where}: mod entry must be a string or a table")


def parse_suite(data: dict[str, Any], *, source: Path | None = None) -> SuiteConfig:
    """Validate a decoded suite manifest."""
    where = str(source) if source else "suite"

    for required in ("name", "minecraft_version", "loader", "scenarios"):
        if required not in data:
            raise ConfigError(f"{where}: missing required field {required!r}")

    try:
        loader = Loader(data["loader"])
    except ValueError:
        raise ConfigError(
            f"{where}: unknown loader {data['loader']!r}; "
            f"expected one of {[l.value for l in Loader]}"
        ) from None

    raw_variants = data.get("variants") or []
    if not raw_variants:
        raise ConfigError(f"{where}: at least one variant is required")

    variants: list[Variant] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_variants):
        if not isinstance(raw, dict) or "name" not in raw:
            raise ConfigError(f"{where}: variants[{index}] needs a 'name'")
        name = raw["name"]
        if name in seen:
            raise ConfigError(f"{where}: duplicate variant name {name!r}")
        seen.add(name)
        variants.append(
            Variant(
                name=name,
                mods=tuple(
                    _parse_mod(m, f"{where}: variants[{index}]")
                    for m in raw.get("mods", [])
                ),
                jvm_args=tuple(raw.get("jvm_args", ())),
                game_settings=dict(raw.get("game_settings", {})),
            )
        )

    baseline = data.get("baseline")
    if baseline is None:
        # Infer the mod-free variant as baseline; require it to be unambiguous,
        # since silently picking one of several would make every comparison in
        # the suite quietly relative to an arbitrary choice.
        empties = [v.name for v in variants if v.is_baseline]
        if len(empties) != 1:
            raise ConfigError(
                f"{where}: 'baseline' must be set explicitly "
                f"(found {len(empties)} mod-free variants)"
            )
        baseline = empties[0]
    elif baseline not in seen:
        raise ConfigError(f"{where}: baseline {baseline!r} is not a declared variant")

    scenarios = tuple(data["scenarios"])
    if not scenarios:
        raise ConfigError(f"{where}: at least one scenario is required")

    runs_per_cell = int(data.get("runs_per_cell", DEFAULT_RUNS_PER_CELL))
    if runs_per_cell < 1:
        raise ConfigError(f"{where}: runs_per_cell must be positive")

    try:
        preset = Preset(data.get("preset", Preset.FULL.value))
    except ValueError:
        raise ConfigError(
            f"{where}: unknown preset {data.get('preset')!r}; "
            f"expected one of {[p.value for p in Preset]}"
        ) from None

    try:
        order = OrderStrategy(data.get("order", OrderStrategy.INTERLEAVED.value))
    except ValueError:
        raise ConfigError(
            f"{where}: unknown order {data.get('order')!r}; "
            f"expected one of {[o.value for o in OrderStrategy]}"
        ) from None

    interactions: list[tuple[str, ...]] = []
    for group in data.get("interactions", []) or []:
        members = tuple(group)
        unknown = [m for m in members if m not in seen]
        if unknown:
            raise ConfigError(
                f"{where}: interaction group references undeclared variants: "
                f"{', '.join(unknown)}"
            )
        interactions.append(members)

    rope = float(data.get("rope", DEFAULT_ROPE))
    if not 0.0 < rope < 1.0:
        raise ConfigError(f"{where}: rope must be in (0, 1), got {rope}")

    return SuiteConfig(
        name=data["name"],
        minecraft_version=str(data["minecraft_version"]),
        loader=loader,
        loader_version=data.get("loader_version"),
        variants=tuple(variants),
        scenarios=scenarios,
        baseline=baseline,
        runs_per_cell=runs_per_cell,
        preset=preset,
        order=order,
        rope=rope,
        seed=int(data.get("seed", 0)),
        interactions=tuple(interactions),
        heap_mb=int(data.get("heap_mb", 4096)),
        source_path=source,
    )


def load_suite(path: str | Path) -> SuiteConfig:
    """Load a suite manifest from TOML or JSON."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"suite manifest does not exist: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            data = tomllib.loads(text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: could not parse: {exc}") from exc

    return parse_suite(data, source=path)
