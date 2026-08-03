"""World fingerprinting: proving two runs measured the same world.

[METHODOLOGY.md](../../docs/METHODOLOGY.md) §7 makes a normative claim — that
runs whose worlds differ are never pooled — and this is what makes it true. A
scenario ships a seed and a setup script rather than a world save, so the world
is generated on the operator's machine; that keeps mcbench clear of
redistributing anything, but it also means the world is an *output* of the run,
not a fixed input. Two variants can therefore silently measure different
terrain, and averaging across that is not a comparison at all.

**What is hashed, and what deliberately is not.** Only block content: the block
state palette, the packed block indices, and biomes. Everything else in a chunk
— entities, block-entity contents, tick lists, lighting, structure references,
inhabited time — is excluded, because those vary between two runs of the *same*
variant. Random ticks fire differently, mobs spawn in different places, and
lighting is recomputed. Hashing them would flag every run as mismatched, and a
check that always fires is a check nobody reads.

**Why the harness computes this rather than the probe.** Reading the saved
region files needs no game API at all, so it costs the adapter SPI nothing and
works identically on every version and platform — including the ones with no
adapter. It also runs after the game has exited, so it cannot perturb the
measurement it exists to qualify.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .nbt import NbtError, decompress_chunk, parse_nbt

__all__ = [
    "WorldError",
    "ChunkRef",
    "WorldFingerprint",
    "fingerprint_world",
    "iter_region_chunks",
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

    def __str__(self) -> str:
        suffix = "" if self.complete else f" ({len(self.unreadable)} unreadable)"
        return f"{self.sha256[:16]}… over {self.chunks} chunks{suffix}"


def iter_region_chunks(path: Path) -> Iterator[tuple[ChunkRef, dict[str, Any]]]:
    """Yield ``(ChunkRef, root_compound)`` for every chunk present in a region.

    Absent chunks — the common case near a world's edge — are skipped silently.
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

    * 1.18 and later — ``sections`` at the root, each with ``block_states``
      (``palette`` + ``data``) and ``biomes``.
    * 1.13 to 1.17 — ``Level.Sections``, each with ``Palette`` + ``BlockStates``.

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
        world_dir: The world save directory — the one containing ``level.dat``.
        dimension: Which region directory to read. ``"region"`` is the
            overworld; the Nether and End live under ``DIM-1`` and ``DIM1``.

    Chunk digests are combined by sorting rather than by file order, so the
    result does not depend on the order the operating system returned the region
    files in — which is not stable across filesystems and would otherwise make
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
