"""World fingerprinting: proving two runs measured the same world.

[METHODOLOGY.md](../../docs/METHODOLOGY.md) §7 claims that runs whose worlds
differ are never pooled, and this is what makes the claim true. A scenario ships
a seed and a setup script rather than a world save, so the world is generated on
the operator's machine. That keeps mcbench clear of redistributing anything, but
it also means the world is an output of the run rather than a fixed input. Two
variants can therefore measure different terrain, and averaging across that is
not a comparison.

**What is hashed, and what deliberately is not.** Only block content: the block
state palette, the packed block indices, and biomes. Entities, block-entity
contents, tick lists, lighting, structure references and inhabited time are all
excluded, because they vary between two runs of the same variant. Random ticks
fire differently, mobs spawn in different places, and lighting is recomputed.
Hashing them would flag every run as mismatched.

**Why the harness computes this rather than the probe.** Reading the saved
region files needs no game API, so it costs the adapter SPI nothing and works
identically on every version and platform, including the ones with no adapter.
It also runs after the game has exited, so it cannot perturb the measurement it
exists to qualify.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .nbt import Byte, Double, Int, Long, NbtError, decompress_chunk, parse_nbt, write_nbt

__all__ = [
    "WorldError",
    "ChunkRef",
    "WorldFingerprint",
    "fingerprint_world",
    "iter_region_chunks",
    "level_dat",
    "create_world",
]

SECTOR_BYTES = 4096
HEADER_SECTORS = 2

#: Guard on a region file's declared chunk extent. A location entry is an
#: offset in 4 KiB sectors and a length in sectors; a corrupt or hostile header
#: can point far outside the file.
MAX_CHUNK_SECTORS = 4096


class WorldError(RuntimeError):
    """A world directory could not be fingerprinted."""


@dataclass(frozen=True)
class ChunkRef:
    """One chunk's coordinates, in chunk units."""

    x: int
    z: int


@dataclass
class WorldFingerprint:
    """A hash over a world's block content, with enough context to explain it."""

    sha256: str
    chunks: int
    regions: int
    #: Chunks that exist but could not be read, with the reason. Reported rather
    #: than skipped: a fingerprint computed over half a world that silently
    #: matched another half-world would be worse than no fingerprint.
    unreadable: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unreadable

    @property
    def usable(self) -> bool:
        """Whether this hash identifies a world well enough to pool runs on it.

        Zero chunks hashes to the digest of nothing, the same constant for
        every empty world. Two runs that generated no terrain would agree on it
        and be pooled on the strength of a comparison that read nothing.
        """
        return self.complete and self.chunks > 0

    def __str__(self) -> str:
        if not self.chunks:
            return "no chunks read"
        suffix = "" if self.complete else f" ({len(self.unreadable)} unreadable)"
        return f"{self.sha256[:16]}… over {self.chunks} chunks{suffix}"


def iter_region_chunks(path: Path) -> Iterator[tuple[ChunkRef, dict[str, Any]]]:
    """Yield ``(ChunkRef, root_compound)`` for every chunk present in a region.

    Absent chunks, the common case near a world's edge, are skipped silently.
    A chunk that is present but unreadable raises, so the caller can record it
    rather than quietly fingerprint a partial world.
    """
    data = path.read_bytes()
    if len(data) < SECTOR_BYTES * HEADER_SECTORS:
        if not data:
            # A zero-length region file is what a freshly created region looks
            # like before anything is saved into it. Not an error.
            return
        raise WorldError(f"{path.name}: shorter than its own header")

    try:
        region_x, region_z = _region_coords(path.name)
    except ValueError as exc:
        raise WorldError(f"{path.name}: {exc}") from None

    for index in range(1024):
        entry = data[index * 4:index * 4 + 4]
        offset = int.from_bytes(entry[:3], "big")
        sectors = entry[3]
        if offset == 0 or sectors == 0:
            continue
        if sectors > MAX_CHUNK_SECTORS:
            raise WorldError(f"{path.name}: chunk {index} claims {sectors} sectors")

        start = offset * SECTOR_BYTES
        end = start + sectors * SECTOR_BYTES
        if end > len(data):
            raise WorldError(
                f"{path.name}: chunk {index} runs past the end of the file"
            )

        length = struct.unpack(">i", data[start:start + 4])[0]
        if length <= 0 or start + 4 + length > len(data):
            raise WorldError(f"{path.name}: chunk {index} has length {length}")

        compression = data[start + 4]
        payload = data[start + 5:start + 4 + length]
        try:
            root = parse_nbt(decompress_chunk(compression, payload))
        except (NbtError, OSError, zlib.error) as exc:
            raise WorldError(f"{path.name}: chunk {index}: {exc}") from None

        chunk = ChunkRef(
            x=region_x * 32 + (index % 32),
            z=region_z * 32 + (index // 32),
        )
        yield chunk, root


def _region_coords(name: str) -> tuple[int, int]:
    """Parse ``r.<x>.<z>.mca``."""
    parts = name.split(".")
    if len(parts) != 4 or parts[0] != "r" or parts[3] != "mca":
        raise ValueError(f"not a region file name: {name!r}")
    return int(parts[1]), int(parts[2])


def _canonical_block_content(root: dict[str, Any]) -> list[Any]:
    """Extract the block content of one chunk, in a stable order.

    Handles both chunk layouts anyone still runs:

    * 1.18 and later: ``sections`` at the root, each with ``block_states``
      (``palette`` + ``data``) and ``biomes``.
    * 1.13 to 1.17: ``Level.Sections``, each with ``Palette`` + ``BlockStates``.

    Before 1.13 blocks were numeric ids in a byte array with no palette. That is
    below the flattening floor the target layer already refuses
    (``targets.py``), so it is not handled here rather than handled wrongly.
    """
    sections = root.get("sections")
    if sections is None:
        level = root.get("Level")
        sections = level.get("Sections") if isinstance(level, dict) else None
    if not isinstance(sections, list):
        return []

    content: list[Any] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        y = section.get("Y")
        modern = section.get("block_states")
        if isinstance(modern, dict):
            palette = modern.get("palette")
            packed = modern.get("data")
            biomes = section.get("biomes")
            biome_palette = (
                biomes.get("palette") if isinstance(biomes, dict) else None
            )
            biome_data = biomes.get("data") if isinstance(biomes, dict) else None
        else:
            palette = section.get("Palette")
            packed = section.get("BlockStates")
            biome_palette = None
            biome_data = None

        if palette is None and packed is None:
            # An empty section: no blocks placed, nothing to distinguish.
            continue

        content.append([
            y,
            _canonical_palette(palette),
            list(packed) if isinstance(packed, list) else None,
            _canonical_palette(biome_palette),
            list(biome_data) if isinstance(biome_data, list) else None,
        ])

    # Sections arrive in save order, which is stable in practice but is not
    # promised by anything. Sorting by Y makes the hash depend on content only.
    content.sort(key=lambda entry: (entry[0] is None, entry[0]))
    return content


def _canonical_palette(palette: Any) -> list[Any] | None:
    """Normalise a palette so equal content hashes equally.

    Property maps are dictionaries, and dictionary order is insertion order in
    the file. Two saves of the same block can therefore serialise
    ``{facing, half}`` and ``{half, facing}``, which would hash differently for
    no physical difference. Sorting removes that.

    The palette's own order is *not* sorted: it is referenced by index from the
    packed data, so reordering it would decouple the two halves.
    """
    if not isinstance(palette, list):
        return None
    canonical: list[Any] = []
    for entry in palette:
        if isinstance(entry, dict):
            name = entry.get("Name", entry.get("name"))
            properties = entry.get("Properties", entry.get("properties"))
            if isinstance(properties, dict):
                canonical.append([name, sorted(properties.items())])
            else:
                canonical.append([name, None])
        else:
            canonical.append([entry, None])
    return canonical


def _digest_chunk(chunk: ChunkRef, root: dict[str, Any]) -> bytes | None:
    content = _canonical_block_content(root)
    if not content:
        # A chunk with no block sections contributes nothing. Including it as an
        # empty entry would make the hash depend on how far the generator had
        # got, which varies with load and is not a property of the world.
        return None
    payload = repr([chunk.x, chunk.z, content]).encode("utf-8")
    return hashlib.sha256(payload).digest()


def fingerprint_world(
    world_dir: str | Path, *, dimension: str = "region"
) -> WorldFingerprint:
    """Hash the block content of a saved world.

    Args:
        world_dir: The world save directory, the one containing ``level.dat``.
        dimension: Which region directory to read. ``"region"`` is the
            overworld; the Nether and End live under ``DIM-1`` and ``DIM1``.

    Chunk digests are combined by sorting rather than by file order, so the
    result does not depend on the order the operating system returned the region
    files in, which is not stable across filesystems and would otherwise make
    two identical worlds hash differently on two machines.
    """
    root = Path(world_dir)
    region_dir = root / dimension
    if not region_dir.is_dir():
        # Try the dimension-folder layout used for custom dimensions.
        alternative = root / dimension / "region"
        if alternative.is_dir():
            region_dir = alternative
        else:
            raise WorldError(f"no {dimension!r} directory under {root}")

    digests: list[bytes] = []
    unreadable: list[str] = []
    regions = sorted(region_dir.glob("r.*.mca"))

    for region in regions:
        try:
            for chunk, chunk_root in iter_region_chunks(region):
                digest = _digest_chunk(chunk, chunk_root)
                if digest is not None:
                    digests.append(digest)
        except (WorldError, OSError) as exc:
            unreadable.append(str(exc))

    digests.sort()
    combined = hashlib.sha256()
    for digest in digests:
        combined.update(digest)

    return WorldFingerprint(
        sha256=combined.hexdigest(),
        chunks=len(digests),
        regions=len(regions),
        unreadable=unreadable,
    )


# --------------------------------------------------------------------------
# Authoring a world for a client run
# --------------------------------------------------------------------------
#
# A client run has to enter a world, and quick-play will only enter one that
# already exists. Letting the client create it instead is not an option: the
# seed, the generator and the game mode would then be whatever the client chose
# at that moment, which is exactly the "two variants measured different terrain"
# failure the fingerprint above exists to catch, introduced deliberately once
# per run.
#
# So the save is authored here from the scenario's own seed. Only `level.dat` is
# written; the terrain is generated by the game from that seed, because mcbench
# never redistributes world data (docs/LICENSING.md).

#: The vanilla dimension set, written explicitly so the world does not depend on
#: whatever the running version happens to default to. These identifiers have
#: been stable since 1.16 introduced the world-generation codec.
_VANILLA_DIMENSIONS = {
    "minecraft:overworld": {
        "type": "minecraft:overworld",
        "generator": {
            "type": "minecraft:noise",
            "settings": "minecraft:overworld",
            "biome_source": {
                "type": "minecraft:multi_noise",
                "preset": "minecraft:overworld",
            },
        },
    },
    "minecraft:the_nether": {
        "type": "minecraft:the_nether",
        "generator": {
            "type": "minecraft:noise",
            "settings": "minecraft:nether",
            "biome_source": {
                "type": "minecraft:multi_noise",
                "preset": "minecraft:nether",
            },
        },
    },
    "minecraft:the_end": {
        "type": "minecraft:the_end",
        "generator": {
            "type": "minecraft:noise",
            "settings": "minecraft:end",
            "biome_source": {"type": "minecraft:the_end"},
        },
    },
}

#: Overworld generator settings per scenario generator name.
#:
#: A scenario names its generator (``scenario.world.generator``) and the world
#: has to be created with it, not merely near it. A ``flat`` scenario opened on
#: default terrain is a different benchmark that will still produce numbers, and
#: nothing downstream would notice.
_OVERWORLD_GENERATORS: dict[str, dict[str, Any]] = {
    "default": {
        "type": "minecraft:noise",
        "settings": "minecraft:overworld",
        "biome_source": {
            "type": "minecraft:multi_noise", "preset": "minecraft:overworld",
        },
    },
    "amplified": {
        "type": "minecraft:noise",
        "settings": "minecraft:amplified",
        "biome_source": {
            "type": "minecraft:multi_noise", "preset": "minecraft:overworld",
        },
    },
    "large_biomes": {
        "type": "minecraft:noise",
        "settings": "minecraft:large_biomes",
        "biome_source": {
            "type": "minecraft:multi_noise", "preset": "minecraft:overworld",
        },
    },
    "flat": {
        "type": "minecraft:flat",
        "settings": {
            "biome": "minecraft:plains",
            "lakes": Byte(0),
            "features": Byte(0),
            "layers": [
                {"block": "minecraft:bedrock", "height": Int(1)},
                {"block": "minecraft:dirt", "height": Int(2)},
                {"block": "minecraft:grass_block", "height": Int(1)},
            ],
        },
    },
    "void": {
        "type": "minecraft:flat",
        "settings": {
            "biome": "minecraft:the_void",
            "lakes": Byte(0),
            "features": Byte(0),
            "layers": [],
        },
    },
}

#: Game rules pinned so the world does not drift underneath a measurement.
#:
#: Each of these is a source of work that varies run to run rather than variant
#: to variant. Daylight and weather change what is rendered; mob spawning
#: changes how many entities tick; random ticks change how much block update
#: work happens. Leaving them on would add variance that belongs to the clock
#: rather than to the mod, and would widen every interval in the report.
_PINNED_GAME_RULES = {
    "doDaylightCycle": "false",
    "doWeatherCycle": "false",
    "doMobSpawning": "false",
    "doFireTick": "false",
    "randomTickSpeed": "0",
    "doTraderSpawning": "false",
    "announceAdvancements": "false",
    "commandBlockOutput": "false",
    "sendCommandFeedback": "false",
}


def level_dat(
    *,
    name: str,
    seed: int,
    generator: str = "default",
    game_type: int = 1,
    generate_features: bool = True,
    difficulty: int = 0,
    spawn: tuple[int, int, int] = (0, 64, 0),
    data_version: int = 3465,
) -> bytes:
    """Build a deterministic ``level.dat`` for a scenario's world.

    ``data_version`` is a floor rather than an exact match for the running game.
    Minecraft upgrades a save whose DataVersion is older than its own, and
    refuses one that is newer; declaring an older version is therefore the safe
    direction, and the default corresponds to 1.20.1.

    ``allowCommands`` is on because a scenario drives the world through
    commands, and ``hardcore`` is off because a benchmark that could end by
    dying would not be a benchmark.
    """
    if game_type not in (0, 1, 2, 3):
        raise WorldError(f"game_type must be 0-3, got {game_type}")
    overworld = _OVERWORLD_GENERATORS.get(generator)
    if overworld is None:
        raise WorldError(
            f"unknown generator {generator!r}; expected one of "
            f"{sorted(_OVERWORLD_GENERATORS)}"
        )

    dimensions = dict(_VANILLA_DIMENSIONS)
    dimensions["minecraft:overworld"] = {
        "type": "minecraft:overworld",
        "generator": overworld,
    }

    data = {
        "DataVersion": Int(data_version),
        "version": Int(19133),
        "initialized": Byte(1),
        "LevelName": name,
        "GameType": Int(game_type),
        "allowCommands": Byte(1),
        "hardcore": Byte(0),
        "Difficulty": Byte(difficulty),
        "DifficultyLocked": Byte(1),
        "raining": Byte(0),
        "thundering": Byte(0),
        "rainTime": Int(1_000_000),
        "thunderTime": Int(1_000_000),
        "clearWeatherTime": Int(1_000_000),
        # Pinned rather than "now": a timestamp would make two instances of the
        # same scenario differ byte for byte for no reason anyone can act on.
        "LastPlayed": Long(0),
        "Time": Long(0),
        "DayTime": Long(6000),  # noon, so lighting is constant and bright
        "SpawnX": Int(spawn[0]),
        "SpawnY": Int(spawn[1]),
        "SpawnZ": Int(spawn[2]),
        "SpawnAngle": Double(0.0),
        "BorderCenterX": Double(0.0),
        "BorderCenterZ": Double(0.0),
        "BorderSize": Double(59_999_968.0),
        "WasModded": Byte(0),
        "WorldGenSettings": {
            "seed": Long(seed),
            "generate_features": Byte(1 if generate_features else 0),
            "bonus_chest": Byte(0),
            "dimensions": dimensions,
        },
        "GameRules": dict(_PINNED_GAME_RULES),
    }
    return write_nbt({"Data": data})


def create_world(
    directory: str | Path,
    *,
    name: str,
    seed: int,
    **level_options: Any,
) -> Path:
    """Create the save directory a client run will be launched into.

    Returns the world directory. Only ``level.dat`` is written; the terrain is
    the game's to generate from the seed, and generating it here would mean
    shipping world data mcbench has no right to redistribute.
    """
    world = Path(directory) / name
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(
        level_dat(name=name, seed=seed, **level_options)
    )
    return world
