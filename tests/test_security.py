"""Security regression tests.

Every case here corresponds to a vulnerability that was present in this codebase
and was found by attacking it rather than by reasoning about it. They exist so
the fixes cannot quietly regress.

The threat model is in docs/SECURITY.md. The short version: mod jars, scenario
files, and result documents are all untrusted input, and the harness *executes*
mod jars by design.
"""

from __future__ import annotations

import zipfile

import pytest

from mcbench.inspect import (
    MAX_ENTRIES,
    MAX_ENTRY_BYTES,
    read_jar,
)
from mcbench.providers.modrinth import ModrinthClient, ModrinthError, ResolvedMod
from mcbench.report import MAX_RUNS_PER_CELL, ResultsError, parse_results_document
from mcbench.runner import compile_plan, write_plan
from mcbench.runner.plan import MAX_COMMAND_LENGTH, PlanError
from mcbench.scenario import parse_scenario


def scenario_with(setup: list[dict]):
    return parse_scenario({
        "id": "sec", "version": "1.0.0", "title": "Security", "side": "server",
        "category": "entity",
        "world": {"seed": 1, "generator": "flat"},
        "measurement": {"warmup": {"min": 1}, "duration": {"full": 1}},
        "setup": setup,
    })


class TestCommandInjection:
    """A scenario is untrusted input: the project encourages sharing them.

    The plan is a newline-delimited file, so a command containing a newline used
    to become several commands on disk — one declared action silently running
    three.
    """

    def test_newline_in_a_command_is_refused(self):
        with pytest.raises(PlanError, match="forbidden control character"):
            compile_plan(scenario_with([
                {"op": "command", "value": "say hello\nop @a"}
            ]))

    def test_carriage_return_is_refused(self):
        with pytest.raises(PlanError, match="forbidden control character"):
            compile_plan(scenario_with([
                {"op": "command", "value": "say hi\rop @a"}
            ]))

    def test_null_byte_is_refused(self):
        with pytest.raises(PlanError, match="forbidden control character"):
            compile_plan(scenario_with([{"op": "command", "value": "say a\x00b"}]))

    def test_injection_never_reaches_the_written_plan(self, tmp_path):
        """The end-to-end check: nothing extra may appear on disk."""
        with pytest.raises(PlanError):
            write_plan(
                scenario_with([{"op": "command", "value": "say x\nop @a"}]),
                tmp_path,
            )
        assert not (tmp_path / "setup.txt").exists()

    def test_injection_through_a_block_name_is_also_refused(self):
        """Defence in depth: the command op is not the only path to a line.

        Structure templates, dialect spellings and interpolated coordinates all
        reach the same file, so every emitted line is swept — a single unchecked
        path would defeat the check.
        """
        with pytest.raises(PlanError, match="forbidden control character"):
            compile_plan(scenario_with([{
                "op": "setblock", "x": 0, "y": 0, "z": 0,
                "block": "minecraft:stone\nop @a",
            }]))

    def test_an_over_long_command_is_refused(self):
        # Minecraft truncates over-long commands, which silently executes a
        # different command than the scenario declared.
        with pytest.raises(PlanError, match="would be truncated"):
            compile_plan(scenario_with([
                {"op": "command", "value": "say " + "a" * (MAX_COMMAND_LENGTH + 10)}
            ]))

    def test_ordinary_commands_still_compile(self):
        plan = compile_plan(scenario_with([
            {"op": "command", "value": "/time set 6000"},
            {"op": "wait", "ticks": 20},
        ]))
        assert "time set 6000" in plan.setup
        assert "@wait 20" in plan.setup


class TestHostileArchives:
    """Inspection is often the first thing run against a pack of unknown origin.

    It must survive a hostile jar rather than be the thing that falls over.
    """

    def test_a_zip_bomb_is_refused_not_read(self, tmp_path):
        path = tmp_path / "bomb.jar"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"b","version":"1"}')
            archive.writestr("mixin/Big.class", b"\0" * (MAX_ENTRY_BYTES * 4))

        meta = read_jar(path)
        # Metadata still read; only the hostile entry is refused.
        assert meta.mod_id == "b"
        assert any("limit" in e or "bomb" in e for e in meta.errors)

    def test_an_extreme_compression_ratio_is_refused(self, tmp_path):
        path = tmp_path / "ratio.jar"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"r","version":"1"}')
            # Highly compressible and over the per-entry cap.
            archive.writestr("mixin/R.class", b"A" * (MAX_ENTRY_BYTES + 1024))
        meta = read_jar(path)
        assert meta.errors

    def test_an_archive_with_absurdly_many_entries_is_refused(self, tmp_path):
        path = tmp_path / "many.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"m","version":"1"}')
            for index in range(MAX_ENTRIES + 5):
                archive.writestr(f"f{index}.txt", "x")
        meta = read_jar(path)
        assert any("entries" in e for e in meta.errors)

    def test_a_truncated_jar_does_not_raise(self, tmp_path):
        path = tmp_path / "torn.jar"
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
        meta = read_jar(path)
        assert meta.errors

    def test_a_normal_jar_is_unaffected(self, tmp_path):
        path = tmp_path / "fine.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"ok","version":"1"}')
        meta = read_jar(path)
        assert meta.mod_id == "ok"
        assert meta.errors == ()


def mod_at(url: str, filename: str = "mod.jar") -> ResolvedMod:
    return ResolvedMod(
        project_id="p", project_slug="s", version_id="v", version_number="1.0",
        filename=filename, url=url, sha512="a" * 128, size=1,
        loaders=(), game_versions=(),
    )


class TestDownloadHardening:
    """The hash check runs *after* the request, so the request itself is the risk.

    A compromised or spoofed API response could point the URL at an internal
    address, and the fetch is issued from inside whatever network the harness
    runs on.
    """

    def test_allows_the_modrinth_cdn(self):
        ModrinthClient._check_download_url("https://cdn.modrinth.com/data/x/mod.jar")

    def test_refuses_plain_http(self):
        with pytest.raises(ModrinthError, match="non-HTTPS"):
            ModrinthClient._check_download_url("http://cdn.modrinth.com/data/x/mod.jar")

    def test_refuses_an_unexpected_host(self):
        with pytest.raises(ModrinthError, match="unexpected host"):
            ModrinthClient._check_download_url("https://evil.example.com/payload.jar")

    def test_refuses_a_link_local_metadata_address(self):
        # The canonical cloud SSRF target.
        with pytest.raises(ModrinthError, match="unexpected host"):
            ModrinthClient._check_download_url("https://169.254.169.254/latest/meta-data/")

    def test_refuses_a_userinfo_prefixed_host(self):
        """`https://cdn.modrinth.com@evil.test/x` resolves to evil.test.

        Parsing the hostname rather than matching the string is what makes this
        safe; a substring check would have been fooled.
        """
        with pytest.raises(ModrinthError, match="unexpected host"):
            ModrinthClient._check_download_url(
                "https://cdn.modrinth.com@evil.example.com/payload.jar"
            )

    def test_cache_filename_cannot_escape_the_cache_directory(self):
        name = ModrinthClient._safe_cache_name(
            mod_at("https://cdn.modrinth.com/a", "../../etc/passwd")
        )
        assert "/" not in name
        assert ".." not in name

    def test_cache_filename_strips_hostile_characters(self):
        name = ModrinthClient._safe_cache_name(
            mod_at("https://cdn.modrinth.com/a", "a b;rm -rf /.jar")
        )
        assert " " not in name and ";" not in name

    def test_cache_filename_is_bounded(self):
        name = ModrinthClient._safe_cache_name(
            mod_at("https://cdn.modrinth.com/a", "x" * 5000)
        )
        assert len(name) < 200

    def test_cache_filename_survives_an_empty_name(self):
        assert ModrinthClient._safe_cache_name(mod_at("https://cdn.modrinth.com/a", ""))


class TestResultDocumentValidation:
    """Result documents are exchanged, and a corpus would ingest them from strangers.

    The dangerous case is not a crash but a *non-finite* value: JSON has no
    infinity, yet ``1e400`` parses to one and would travel through aggregation
    into a report whose own JSON contains ``Infinity`` — invalid per the spec,
    unreadable by strict consumers, and presented as though it were a
    measurement.
    """

    def _doc(self, **cells):
        return {"suite": "s", "baseline": "base", "cells": cells}

    def test_accepts_a_well_formed_document(self):
        suite, baseline, cells = parse_results_document(self._doc(**{
            "sc/base": [{"values": {"frametime_mean_ms": 16.7}, "flags": []}],
        }))
        assert baseline == "base"
        assert cells["sc/base"][0]["values"]["frametime_mean_ms"] == 16.7

    def test_refuses_a_non_finite_value(self):
        with pytest.raises(ResultsError, match="not a finite measurement"):
            parse_results_document(self._doc(**{
                "sc/base": [{"values": {"frametime_mean_ms": float("inf")}}],
            }))

    def test_refuses_nan(self):
        with pytest.raises(ResultsError, match="not a finite measurement"):
            parse_results_document(self._doc(**{
                "sc/base": [{"values": {"frametime_mean_ms": float("nan")}}],
            }))

    def test_a_non_finite_value_is_refused_even_when_outliers_would_hide_it(self):
        """The MAD filter happened to absorb a single infinity, by accident.

        Relying on that would be relying on a filter built for a different
        purpose, and it left the reader seeing 'n=4' rather than 'your data
        contained infinity'.
        """
        runs = [{"values": {"frametime_mean_ms": 16.7}} for _ in range(5)]
        runs[0] = {"values": {"frametime_mean_ms": float("inf")}}
        with pytest.raises(ResultsError, match="not a finite measurement"):
            parse_results_document(self._doc(**{"sc/base": runs}))

    def test_refuses_a_string_where_a_number_belongs(self):
        with pytest.raises(ResultsError, match="expected a number"):
            parse_results_document(self._doc(**{
                "sc/base": [{"values": {"frametime_mean_ms": "fast"}}],
            }))

    def test_refuses_a_boolean_masquerading_as_a_number(self):
        with pytest.raises(ResultsError, match="expected a number"):
            parse_results_document(self._doc(**{
                "sc/base": [{"values": {"frametime_mean_ms": True}}],
            }))

    def test_requires_a_baseline(self):
        with pytest.raises(ResultsError, match="baseline"):
            parse_results_document({"suite": "s", "cells": {}})

    def test_refuses_a_malformed_cell_key(self):
        with pytest.raises(ResultsError, match="malformed cell key"):
            parse_results_document(self._doc(**{"no-slash-here": []}))

    def test_bounds_the_number_of_runs(self):
        # An unbounded list is a cheap way to exhaust memory in whatever ingests
        # a submission.
        runs = [{"values": {"frametime_mean_ms": 1.0}}] * (MAX_RUNS_PER_CELL + 1)
        with pytest.raises(ResultsError, match="exceeds"):
            parse_results_document(self._doc(**{"sc/base": runs}))

    def test_refuses_a_non_object_document(self):
        with pytest.raises(ResultsError, match="must be a JSON object"):
            parse_results_document([1, 2, 3])

    def test_refuses_malformed_flags(self):
        with pytest.raises(ResultsError, match="flags"):
            parse_results_document(self._doc(**{
                "sc/base": [{"values": {}, "flags": [{"not": "a string"}]}],
            }))
