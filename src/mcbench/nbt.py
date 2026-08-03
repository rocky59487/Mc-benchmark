"""A minimal NBT reader, and just enough writer to author a ``level.dat``.

The reader covers what a world fingerprint needs: enough of the format to walk a
chunk's block data and nothing else. Tags are decoded lazily where skipping is
cheaper than materialising: a region file holds up to 1024 chunks and a benchmark
run touches several region files per instance.

The writer exists for one job: a client run has to be launched *into* a world,
and quick-play will only enter a world that already exists on disk. Rather than
letting the game create one, whose seed and settings would be whatever the client
felt like and would differ between variants, the harness authors the save itself
from the scenario's seed. Tag types are explicit rather than inferred,
because Python's ``int`` covers four different NBT tags and guessing wrong
produces a file the game rejects with no useful message.

Pure standard library, like the rest of mcbench. NBT is a small enough format
that a dependency would cost more than it saves, and the project's claim that a
stock interpreter can verify a published result is worth more than the hundred
lines saved.
"""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass
from typing import Any

__all__ = [
    "NbtError",
    "TagType",
    "parse_nbt",
    "decompress_chunk",
    "Byte",
    "Short",
    "Int",
    "Long",
    "Float",
    "Double",
    "write_nbt",
]


class NbtError(ValueError):
    """Malformed NBT, or a tag type this reader does not implement."""


class TagType:
    END = 0
    BYTE = 1
    SHORT = 2
    INT = 3
    LONG = 4
    FLOAT = 5
    DOUBLE = 6
    BYTE_ARRAY = 7
    STRING = 8
    LIST = 9
    COMPOUND = 10
    INT_ARRAY = 11
    LONG_ARRAY = 12


#: Ceiling on a single decompressed chunk. Region files are untrusted input in
#: the same sense mod jars are (see docs/SECURITY.md): a benchmark may run a
#: world produced by someone else's worldgen mod, and a chunk that inflates to
#: gigabytes would take the harness down between runs. Real chunks are well
#: under a megabyte; this is two orders of magnitude of headroom.
MAX_CHUNK_BYTES = 64 * 1024 * 1024

#: Ceiling on nesting depth. NBT is self-describing and recursive, so a crafted
#: file can otherwise drive the parser into a stack overflow.
#:
#: Set well below Python's own recursion limit rather than near it: each NBT
#: level costs two Python frames, so a ceiling of 512 would still hit
#: RecursionError first and raise the wrong error from the wrong place. Real
#: chunk data nests under ten deep.
MAX_DEPTH = 64


@dataclass
class _Cursor:
    data: bytes
    offset: int = 0
    depth: int = 0

    def take(self, count: int) -> bytes:
        end = self.offset + count
        if count < 0 or end > len(self.data):
            raise NbtError(
                f"truncated at offset {self.offset}: wanted {count} bytes, "
                f"{len(self.data) - self.offset} remain"
            )
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def unpack(self, fmt: str) -> Any:
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.take(size))[0]


def _read_string(cursor: _Cursor) -> str:
    length = cursor.unpack(">H")
    raw = cursor.take(length)
    # Minecraft writes modified UTF-8. The difference only shows up for NUL and
    # for supplementary characters, neither of which appears in block or biome
    # identifiers; surrogatepass keeps a hostile file from raising here.
    return raw.decode("utf-8", errors="surrogatepass")


def _read_payload(cursor: _Cursor, tag: int) -> Any:
    if cursor.depth > MAX_DEPTH:
        raise NbtError(f"nesting deeper than {MAX_DEPTH}; refusing to recurse")

    if tag == TagType.BYTE:
        return cursor.unpack(">b")
    if tag == TagType.SHORT:
        return cursor.unpack(">h")
    if tag == TagType.INT:
        return cursor.unpack(">i")
    if tag == TagType.LONG:
        return cursor.unpack(">q")
    if tag == TagType.FLOAT:
        return cursor.unpack(">f")
    if tag == TagType.DOUBLE:
        return cursor.unpack(">d")
    if tag == TagType.BYTE_ARRAY:
        return cursor.take(max(cursor.unpack(">i"), 0))
    if tag == TagType.STRING:
        return _read_string(cursor)
    if tag == TagType.LIST:
        element = cursor.unpack(">b")
        count = cursor.unpack(">i")
        if count <= 0:
            return []
        if element == TagType.END:
            raise NbtError("list of TAG_End with a non-zero length")
        cursor.depth += 1
        try:
            return [_read_payload(cursor, element) for _ in range(count)]
        finally:
            cursor.depth -= 1
    if tag == TagType.COMPOUND:
        cursor.depth += 1
        try:
            return _read_compound(cursor)
        finally:
            cursor.depth -= 1
    if tag == TagType.INT_ARRAY:
        count = max(cursor.unpack(">i"), 0)
        return list(struct.unpack(f">{count}i", cursor.take(count * 4)))
    if tag == TagType.LONG_ARRAY:
        count = max(cursor.unpack(">i"), 0)
        return list(struct.unpack(f">{count}q", cursor.take(count * 8)))

    raise NbtError(f"unknown tag type {tag} at offset {cursor.offset}")


def _read_compound(cursor: _Cursor) -> dict[str, Any]:
    result: dict[str, Any] = {}
    while True:
        tag = cursor.unpack(">b")
        if tag == TagType.END:
            return result
        name = _read_string(cursor)
        result[name] = _read_payload(cursor, tag)


def parse_nbt(data: bytes) -> dict[str, Any]:
    """Parse an uncompressed NBT document, returning the root compound.

    The root is unwrapped: NBT documents are a named compound whose name is
    almost always empty, and every caller here wants what is inside it.
    """
    cursor = _Cursor(data)
    tag = cursor.unpack(">b")
    if tag != TagType.COMPOUND:
        raise NbtError(f"root tag is {tag}, expected a compound")
    _read_string(cursor)  # root name, conventionally empty
    return _read_compound(cursor)


def decompress_chunk(compression: int, payload: bytes) -> bytes:
    """Decompress one region-file chunk payload.

    Bounded rather than trusting the stream's own length, for the reason given
    on :data:`MAX_CHUNK_BYTES`.
    """
    if compression == 1:
        raw = gzip.decompress(payload)
    elif compression == 2:
        # Incremental so a compression bomb is refused at the ceiling rather
        # than after it has already been allocated.
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(payload, MAX_CHUNK_BYTES + 1)
    elif compression == 3:
        raw = payload
    else:
        # 4 is LZ4 and 127 is a custom scheme; both are rare and neither is
        # implementable from the standard library. Saying so beats a wrong hash.
        raise NbtError(
            f"chunk compression scheme {compression} is not supported "
            f"(1=gzip, 2=zlib, 3=none)"
        )

    if len(raw) > MAX_CHUNK_BYTES:
        raise NbtError(
            f"chunk inflates to more than {MAX_CHUNK_BYTES} bytes; refusing"
        )
    return raw


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
#
# Tag types are carried by wrapper classes rather than inferred from Python
# types. A Python ``int`` is a candidate for BYTE, SHORT, INT and LONG, and the
# game reads a mistyped field as garbage or refuses the file outright with a
# message that names neither the field nor the type, so the ambiguity has to be
# resolved by the caller, once, where the meaning is known.


class _Typed(int):
    """An integer that remembers which NBT tag it is."""

    __slots__ = ()
    tag = TagType.INT
    fmt = ">i"


class Byte(_Typed):
    tag = TagType.BYTE
    fmt = ">b"


class Short(_Typed):
    tag = TagType.SHORT
    fmt = ">h"


class Int(_Typed):
    tag = TagType.INT
    fmt = ">i"


class Long(_Typed):
    tag = TagType.LONG
    fmt = ">q"


class _TypedFloat(float):
    __slots__ = ()
    tag = TagType.DOUBLE
    fmt = ">d"


class Float(_TypedFloat):
    tag = TagType.FLOAT
    fmt = ">f"


class Double(_TypedFloat):
    tag = TagType.DOUBLE
    fmt = ">d"


def _write_string(out: bytearray, value: str) -> None:
    encoded = value.encode("utf-8", errors="surrogatepass")
    if len(encoded) > 0xFFFF:
        raise NbtError(f"string of {len(encoded)} bytes exceeds the NBT limit")
    out += struct.pack(">H", len(encoded))
    out += encoded


def _tag_of(value: Any) -> int:
    if isinstance(value, (_Typed, _TypedFloat)):
        return value.tag
    if isinstance(value, bool):
        # Before the int check: bool is a subclass of int, and NBT has no boolean.
        return TagType.BYTE
    if isinstance(value, str):
        return TagType.STRING
    if isinstance(value, dict):
        return TagType.COMPOUND
    if isinstance(value, (list, tuple)):
        return TagType.LIST
    if isinstance(value, (bytes, bytearray)):
        return TagType.BYTE_ARRAY
    raise NbtError(
        f"cannot write {type(value).__name__}; wrap integers in Byte/Short/"
        f"Int/Long and reals in Float/Double so the tag type is explicit"
    )


def _write_payload(out: bytearray, value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise NbtError(f"nesting deeper than {MAX_DEPTH}; refusing to recurse")

    tag = _tag_of(value)
    if tag == TagType.STRING:
        _write_string(out, value)
    elif tag == TagType.COMPOUND:
        for key, item in value.items():
            out.append(_tag_of(item))
            _write_string(out, key)
            _write_payload(out, item, depth + 1)
        out.append(TagType.END)
    elif tag == TagType.LIST:
        items = list(value)
        # An empty list is written as END-typed, which is what the game emits
        # and what its reader expects; writing some other type would make an
        # empty list and a list of that type indistinguishable on the way back.
        element = _tag_of(items[0]) if items else TagType.END
        for item in items:
            if _tag_of(item) != element:
                raise NbtError("NBT lists are homogeneous; got mixed tag types")
        out.append(element)
        out += struct.pack(">i", len(items))
        for item in items:
            _write_payload(out, item, depth + 1)
    elif tag == TagType.BYTE_ARRAY:
        out += struct.pack(">i", len(value))
        out += bytes(value)
    elif isinstance(value, bool):
        out += struct.pack(">b", 1 if value else 0)
    else:
        out += struct.pack(value.fmt, value)


def write_nbt(root: dict[str, Any], *, name: str = "", compress: bool = True) -> bytes:
    """Serialise a compound as a complete NBT document.

    ``compress`` produces the gzip stream every ``level.dat`` on disk is stored
    as; the uncompressed form is useful for round-tripping in tests.
    """
    out = bytearray()
    out.append(TagType.COMPOUND)
    _write_string(out, name)
    _write_payload(out, root)
    data = bytes(out)
    # mtime is pinned so the same world description produces byte-identical
    # bytes; a timestamp in the header would make every instance's level.dat
    # differ and undermine the point of authoring it deterministically.
    return gzip.compress(data, mtime=0) if compress else data
