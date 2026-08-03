"""A minimal NBT reader.

Only what a world fingerprint needs: enough of the format to walk a chunk's
block data and nothing else. There is no writer, and tags are decoded lazily
where skipping is cheaper than materialising — a region file holds up to 1024
chunks and a benchmark run touches several region files per instance.

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

__all__ = ["NbtError", "TagType", "parse_nbt", "decompress_chunk"]


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
#: the same sense mod jars are (see docs/SECURITY.md) — a benchmark may run a
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
