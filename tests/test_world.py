"""Tests for NBT parsing and world fingerprinting.

Fixtures are built by *writing real region files* rather than by mocking the
reader. A fingerprint's whole job is to notice that two worlds differ, and a
test whose input never went through the Anvil format could not tell a correct
implementation from one that agrees with itself.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from mcbench.nbt import MAX_CHUNK_BYTES, NbtError, decompress_chunk, parse_nbt
from mcbench.world import WorldError, fingerprint_world, iter_region_chunks

# --------------------------------------------------------------------------
# A minimal NBT writer, for fixtures only.
# --------------------------------------------------------------------------


def _string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _payload(value) -> tuple[int, bytes]:
    """Serialise a Python value, returning ``(tag_type, payload)``."""
    if isinstance(value, bool):
        return 1, struct.pack(">b", int(value))
    if isinstance(value, int):
        return 3, struct.pack(">i", value)
    if isinstance(value, float):
        return 6, struct.pack(">d", value)
    if isinstance(value, str):
        return 8, _string(value)
    if isinstance(value, dict):
        return 10, _compound(value)
    if isinstance(value, LongArray):
        return 12, struct.pack(">i", len(value.items)) + b"".join(
            struct.pack(">q", v) for v in value.items
        )
    if isinstance(value, list):
        if not value:
            return 9, struct.pack(">bi", 0, 0)
        tags = [_payload(v) for v in value]
        element = tags[0][0]
        assert all(t == element for t, _ in tags), "heterogeneous NBT list"
        return 9, struct.pack(">bi", element, len(tags)) + b"".join(
            p for _, p in tags
        )
    raise TypeError(f"no NBT encoding for {type(value)}")


class LongArray:
    """Marker so a list of ints can be written as TAG_Long_Array."""

    def __init__(self, items):
        self.items = list(items)


def _compound(mapping: dict) -> bytes:
    out = b""
    for key, value in mapping.items():
        tag, payload = _payload(value)
        out += struct.pack(">b", tag) + _string(key) + payload
    return out + b"\x00"


def _document(root: dict) -> bytes:
    return struct.pack(">b", 10) + _string("") + _compound(root)


def _write_region(path, chunks: dict[tuple[int, int], dict]) -> None:
    """Write ``r.0.0.mca`` containing the given chunks, keyed by (local x, z)."""
    locations = bytearray(4096)
    timestamps = bytearray(4096)
    body = b""
    sector = 2

    for (x, z), root in sorted(chunks.items()):
        compressed = zlib.compress(_document(root))
        payload = struct.pack(">i", len(compressed) + 1) + b"\x02" + compressed
        padded = payload + b"\x00" * (-len(payload) % 4096)
        count = len(padded) // 4096

        index = (x % 32) + (z % 32) * 32
        locations[index * 4:index * 4 + 3] = sector.to_bytes(3, "big")
        locations[index * 4 + 3] = count
        timestamps[index * 4:index * 4 + 4] = (1).to_bytes(4, "big")

        body += padded
        sector += count

    path.write_bytes(bytes(locations) + bytes(timestamps) + body)


def _chunk(palette_name: str = "minecraft:stone", *, entities=None, data=None):
    """A modern (1.18+) chunk with one block section."""
    root = {
        "DataVersion": 3955,
        "sections": [{
            "Y": 0,
            "block_states": {
                "palette": [
                    {"Name": "minecraft:air"},
                    {"Name": palette_name, "Properties": {"facing": "north"}},
                ],
                "data": LongArray(data if data is not None else [0x0123456789ABCDEF]),
            },
            "biomes": {"palette": ["minecraft:plains"]},
        }],
    }
    if entities is not None:
        root["block_entities"] = entities
    return root


def _world(tmp_path, name: str, chunks: dict[tuple[int, int], dict]):
    world = tmp_path / name
    region = world / "region"
    region.mkdir(parents=True)
    _write_region(region / "r.0.0.mca", chunks)
    return world


class TestNbt:
    def test_round_trips_every_type_the_reader_claims(self):
        original = {
            "byte": True, "int": -7, "double": 1.5, "text": "minecraft:stone",
            "list": [1, 2, 3], "nested": {"inner": "x"},
            "longs": LongArray([1, -2, 3]),
        }
        parsed = parse_nbt(_document(original))
        assert parsed["int"] == -7
        assert parsed["text"] == "minecraft:stone"
        assert parsed["nested"]["inner"] == "x"
        assert parsed["longs"] == [1, -2, 3]

    def test_truncation_is_an_error_not_a_partial_parse(self):
        document = _document({"text": "hello"})
        with pytest.raises(NbtError, match="truncated"):
            parse_nbt(document[:-3])

    def test_a_non_compound_root_is_refused(self):
        with pytest.raises(NbtError, match="root tag"):
            parse_nbt(b"\x08\x00\x00")

    def test_unbounded_nesting_is_refused_rather_than_overflowing_the_stack(self):
        # A crafted file would otherwise take the harness down between runs.
        deep = b""
        for _ in range(2000):
            deep += b"\x0a" + _string("n")
        with pytest.raises(NbtError, match="nesting"):
            parse_nbt(b"\x0a" + _string("") + deep)

    def test_an_unsupported_compression_scheme_says_so(self):
        # Better than a confidently wrong hash over garbage.
        with pytest.raises(NbtError, match="compression scheme 4"):
            decompress_chunk(4, b"whatever")

    def test_a_compression_bomb_is_refused(self):
        bomb = zlib.compress(b"\x00" * (MAX_CHUNK_BYTES + 1024))
        assert len(bomb) < 100_000
        with pytest.raises(NbtError, match="refusing"):
            decompress_chunk(2, bomb)


class TestFingerprint:
    def test_identical_worlds_hash_identically(self, tmp_path):
        a = _world(tmp_path, "a", {(0, 0): _chunk(), (1, 0): _chunk()})
        b = _world(tmp_path, "b", {(0, 0): _chunk(), (1, 0): _chunk()})
        assert fingerprint_world(a).sha256 == fingerprint_world(b).sha256
        assert fingerprint_world(a).chunks == 2

    def test_a_single_changed_block_changes_the_hash(self, tmp_path):
        a = _world(tmp_path, "a", {(0, 0): _chunk("minecraft:stone")})
        b = _world(tmp_path, "b", {(0, 0): _chunk("minecraft:deepslate")})
        assert fingerprint_world(a).sha256 != fingerprint_world(b).sha256

    def test_changed_block_indices_change_the_hash(self, tmp_path):
        # The palette can be identical while the blocks placed from it differ.
        a = _world(tmp_path, "a", {(0, 0): _chunk(data=[1])})
        b = _world(tmp_path, "b", {(0, 0): _chunk(data=[2])})
        assert fingerprint_world(a).sha256 != fingerprint_world(b).sha256

    def test_entities_do_not_affect_the_hash(self, tmp_path):
        # The property the whole design turns on. Mobs spawn in different
        # places on every run of the same variant; if that moved the hash,
        # every run would be flagged as a mismatch and nobody would read the
        # flag again.
        a = _world(tmp_path, "a", {(0, 0): _chunk(entities=[])})
        b = _world(tmp_path, "b", {(0, 0): _chunk(entities=[
            {"id": "minecraft:chest", "x": 1, "y": 2, "z": 3},
        ])})
        assert fingerprint_world(a).sha256 == fingerprint_world(b).sha256

    def test_property_order_does_not_affect_the_hash(self, tmp_path):
        # Dictionary order is insertion order in the file, so two saves of the
        # same block can serialise its properties in different orders.
        forward = {"DataVersion": 3955, "sections": [{
            "Y": 0,
            "block_states": {
                "palette": [{"Name": "minecraft:furnace",
                             "Properties": {"facing": "north", "lit": "false"}}],
                "data": LongArray([0]),
            },
        }]}
        reverse = {"DataVersion": 3955, "sections": [{
            "Y": 0,
            "block_states": {
                "palette": [{"Name": "minecraft:furnace",
                             "Properties": {"lit": "false", "facing": "north"}}],
                "data": LongArray([0]),
            },
        }]}
        a = _world(tmp_path, "a", {(0, 0): forward})
        b = _world(tmp_path, "b", {(0, 0): reverse})
        assert fingerprint_world(a).sha256 == fingerprint_world(b).sha256

    def test_section_order_does_not_affect_the_hash(self, tmp_path):
        def world(order):
            return {"DataVersion": 3955, "sections": [
                {"Y": y, "block_states": {
                    "palette": [{"Name": f"minecraft:b{y}"}],
                    "data": LongArray([y]),
                }} for y in order
            ]}
        a = _world(tmp_path, "a", {(0, 0): world([0, 1, 2])})
        b = _world(tmp_path, "b", {(0, 0): world([2, 0, 1])})
        assert fingerprint_world(a).sha256 == fingerprint_world(b).sha256

    def test_chunk_position_is_part_of_the_hash(self, tmp_path):
        # The same terrain in a different place is a different world, and a
        # fingerprint that ignored position would call an offset world equal.
        a = _world(tmp_path, "a", {(0, 0): _chunk()})
        b = _world(tmp_path, "b", {(5, 7): _chunk()})
        assert fingerprint_world(a).sha256 != fingerprint_world(b).sha256

    def test_the_legacy_chunk_layout_is_understood(self, tmp_path):
        # 1.13 to 1.17 put sections under Level with capitalised keys.
        legacy = {"DataVersion": 2586, "Level": {"Sections": [{
            "Y": 0,
            "Palette": [{"Name": "minecraft:stone"}],
            "BlockStates": LongArray([42]),
        }]}}
        world = _world(tmp_path, "legacy", {(0, 0): legacy})
        result = fingerprint_world(world)
        assert result.chunks == 1
        assert result.complete

    def test_empty_sections_do_not_depend_on_generator_progress(self, tmp_path):
        # A chunk saved before its sections were populated must not contribute,
        # or the hash would depend on machine load rather than on the world.
        blank = {"DataVersion": 3955, "sections": [{"Y": 0}]}
        world = _world(tmp_path, "blank", {(0, 0): blank})
        assert fingerprint_world(world).chunks == 0

    def test_an_unreadable_chunk_is_reported_not_skipped(self, tmp_path):
        world = _world(tmp_path, "broken", {(0, 0): _chunk()})
        region = world / "region" / "r.0.0.mca"
        data = bytearray(region.read_bytes())
        # Corrupt the compressed payload, leaving the header intact.
        data[8192 + 8] ^= 0xFF
        region.write_bytes(bytes(data))

        result = fingerprint_world(world)
        assert not result.complete
        assert result.unreadable
        assert "r.0.0.mca" in result.unreadable[0]

    def test_a_missing_region_directory_is_a_clear_error(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(WorldError, match="no 'region' directory"):
            fingerprint_world(tmp_path / "empty")

    def test_a_zero_length_region_file_is_not_an_error(self, tmp_path):
        # What a freshly created region looks like before anything is saved.
        world = tmp_path / "fresh"
        (world / "region").mkdir(parents=True)
        (world / "region" / "r.0.0.mca").write_bytes(b"")
        result = fingerprint_world(world)
        assert result.chunks == 0
        assert result.complete

    def test_a_chunk_pointing_past_the_end_is_refused(self, tmp_path):
        world = _world(tmp_path, "w", {(0, 0): _chunk()})
        region = world / "region" / "r.0.0.mca"
        data = bytearray(region.read_bytes())
        data[0:3] = (9999).to_bytes(3, "big")
        region.write_bytes(bytes(data))
        with pytest.raises(WorldError, match="past the end"):
            list(iter_region_chunks(region))


class TestPoolingRule:
    """METHODOLOGY §7: runs whose worlds differ are never pooled."""

    def _outcome(self, scenario, variant, fingerprint, position=0):
        from mcbench.metrics import RunMetrics
        from mcbench.planner import Cell, PlannedRun
        from mcbench.runner import RunOutcome

        return RunOutcome(
            planned=PlannedRun(
                cell=Cell(scenario=scenario, variant=variant),
                replicate=position, position=position, round_index=0,
            ),
            metrics=RunMetrics(values={"fps_avg": 100.0}, sample_count=5_000),
            world_fingerprint=fingerprint,
        )

    def test_a_variant_that_changed_worldgen_is_flagged(self):
        from mcbench.metrics import RunFlag
        from mcbench.runner import flag_world_mismatches

        outcomes = [
            self._outcome("s", "base", "aaa", 0),
            self._outcome("s", "base", "aaa", 1),
            self._outcome("s", "mod", "bbb", 2),
        ]
        mismatched = flag_world_mismatches(outcomes)

        assert mismatched == {"s": ["bbb"]}
        assert RunFlag.WORLD_FINGERPRINT_MISMATCH in outcomes[2].metrics.flags
        assert not outcomes[0].metrics.flags
        # Inadmissible means it cannot reach a published comparison at all.
        assert not outcomes[2].metrics.admissible

    def test_agreeing_runs_are_left_alone(self):
        from mcbench.runner import flag_world_mismatches

        outcomes = [self._outcome("s", "base", "aaa", i) for i in range(4)]
        assert flag_world_mismatches(outcomes) == {}
        assert all(not o.metrics.flags for o in outcomes)

    def test_runs_without_a_fingerprint_are_not_flagged(self):
        # Absence of evidence is not evidence of mismatch. Flagging every run on
        # a platform where the world cannot be read would make the flag mean
        # nothing.
        from mcbench.runner import flag_world_mismatches

        outcomes = [
            self._outcome("s", "base", "aaa", 0),
            self._outcome("s", "mod", "", 1),
        ]
        assert flag_world_mismatches(outcomes) == {}
        assert not outcomes[1].metrics.flags

    def test_scenarios_are_compared_independently(self):
        # Two scenarios legitimately have different worlds; that is not a
        # mismatch, it is the point of having more than one scenario.
        from mcbench.runner import flag_world_mismatches

        outcomes = [
            self._outcome("a", "base", "aaa", 0),
            self._outcome("b", "base", "bbb", 1),
        ]
        assert flag_world_mismatches(outcomes) == {}

    def test_the_reference_world_is_the_majority_not_the_first(self):
        # Run order must not decide which world counts as correct; a single
        # early bad run would otherwise condemn every good one after it.
        from mcbench.metrics import RunFlag
        from mcbench.runner import flag_world_mismatches

        outcomes = [
            self._outcome("s", "base", "odd", 0),
            self._outcome("s", "base", "common", 1),
            self._outcome("s", "mod", "common", 2),
            self._outcome("s", "mod", "common", 3),
        ]
        flag_world_mismatches(outcomes)
        assert RunFlag.WORLD_FINGERPRINT_MISMATCH in outcomes[0].metrics.flags
        assert all(
            RunFlag.WORLD_FINGERPRINT_MISMATCH not in o.metrics.flags
            for o in outcomes[1:]
        )

    def test_the_flag_travels_into_the_analysis_input(self):
        # The check has to sit on the funnel every run passes through, or an
        # analysis path could route around it.
        from mcbench.runner import outcomes_to_cells

        cells = outcomes_to_cells([
            self._outcome("s", "base", "aaa", 0),
            self._outcome("s", "base", "aaa", 1),
            self._outcome("s", "mod", "bbb", 2),
        ])
        assert "world_fingerprint_mismatch" in cells["s/mod"][0]["flags"]
        assert cells["s/base"][0]["world"] == "aaa"
