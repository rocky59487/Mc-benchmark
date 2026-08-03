"""Dependency-closed bisect: the case that produced false interaction claims.

The failure mode is concrete. A mod ``bad`` requires ``library``. Testing
``{bad}`` alone fails to launch for want of the library, and testing
``{library}`` alone fails to reproduce because it is not the culprit. A search
that reads both failures as "does not reproduce" concludes that neither mod is
individually at fault and reports an *interaction* — so two mod authors receive
a bug report about a conflict that does not exist, and the single-mod fault goes
unfixed.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from mcbench.deps import graph_from_metadata
from mcbench.diagnose import Outcome, isolate, leave_one_out
from mcbench.inspect import read_jars
from mcbench.runner.oracle import BenchmarkOracle


def fabric_jar(path: Path, meta: dict) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1, **meta}))
    return path


def pack(tmp_path, mods: dict[str, dict]) -> list:
    paths = [
        fabric_jar(tmp_path / f"{name}.jar", {"id": name, "version": "1", **spec})
        for name, spec in mods.items()
    ]
    return read_jars(paths, scan_mixins=False)


class TestGraph:
    def test_hard_dependencies_become_edges(self, tmp_path):
        mods = pack(tmp_path, {
            "bad": {"depends": {"library": "*"}},
            "library": {},
            "unrelated": {},
        })
        graph = graph_from_metadata(mods)
        assert graph.dependencies_of("bad") == frozenset({"library"})
        assert graph.dependencies_of("library") == frozenset()

    def test_ambient_ids_are_not_edges(self, tmp_path):
        """The loader and the game are not mods a bisect can toggle."""
        mods = pack(tmp_path, {
            "solo": {"depends": {"minecraft": ">=1.21", "fabricloader": "*"}},
        })
        graph = graph_from_metadata(mods)
        assert graph.dependencies_of("solo") == frozenset()
        assert not graph.unresolved

    def test_a_dependency_nothing_provides_is_recorded_as_unresolved(self, tmp_path):
        mods = pack(tmp_path, {"solo": {"depends": {"absent": "*"}}})
        graph = graph_from_metadata(mods)
        assert graph.unresolved["solo"] == frozenset({"absent"})

    def test_an_alias_resolves_to_the_jar_providing_it(self, tmp_path):
        mods = pack(tmp_path, {
            "consumer": {"depends": {"the-api": "*"}},
            "provider": {"provides": ["the-api"]},
        })
        graph = graph_from_metadata(mods)
        closure = graph.closure(["consumer"])
        assert closure.members == ("consumer", "provider")
        assert closure.support == ("provider",)


class TestClosure:
    def test_a_subset_is_expanded_to_something_launchable(self, tmp_path):
        graph = graph_from_metadata(pack(tmp_path, {
            "bad": {"depends": {"library": "*"}},
            "library": {},
        }))
        closure = graph.closure(["bad"])
        assert set(closure.members) == {"bad", "library"}
        assert closure.support == ("library",)
        assert closure.satisfiable

    def test_support_is_reported_apart_from_the_suspects(self, tmp_path):
        """A library pulled in is present and was never independently implicated."""
        graph = graph_from_metadata(pack(tmp_path, {
            "bad": {"depends": {"library": "*"}},
            "library": {},
        }))
        closure = graph.closure(["bad"])
        assert closure.subset == ("bad",)
        assert "library" not in closure.subset

    def test_transitive_dependencies_are_followed(self, tmp_path):
        graph = graph_from_metadata(pack(tmp_path, {
            "top": {"depends": {"middle": "*"}},
            "middle": {"depends": {"bottom": "*"}},
            "bottom": {},
        }))
        assert set(graph.closure(["top"]).members) == {"top", "middle", "bottom"}

    def test_a_cycle_terminates(self, tmp_path):
        graph = graph_from_metadata(pack(tmp_path, {
            "a": {"depends": {"b": "*"}},
            "b": {"depends": {"a": "*"}},
        }))
        assert set(graph.closure(["a"]).members) == {"a", "b"}

    def test_an_unsatisfiable_subset_says_what_is_missing(self, tmp_path):
        graph = graph_from_metadata(pack(tmp_path, {
            "solo": {"depends": {"absent": "*"}},
        }))
        closure = graph.closure(["solo"])
        assert not closure.satisfiable
        assert closure.missing == ("absent",)


class TestIsolationWithDependencies:
    def _oracle(self, culprit, *, runs=7, noise=0.01):
        """A measurement stand-in that only launches loadable configurations."""
        import random

        rng = random.Random(4321)

        def measure(mods, replicate):
            installed = set(mods)
            base = 16.7
            if culprit <= installed:
                base *= 1.35
            return base * (1 + rng.gauss(0, noise))

        return BenchmarkOracle(measure, runs_per_probe=runs)

    def test_a_culprit_behind_a_hard_dependency_is_found_alone(self, tmp_path):
        """The fixture from the issue: `bad` requires `library`.

        Without closure, `{bad}` never launches and `{library}` never
        reproduces, and the search reports the pair as an interaction. With it,
        `bad` is named and `library` is reported as support.
        """
        graph = graph_from_metadata(pack(tmp_path, {
            "bad": {"depends": {"library": "*"}},
            "library": {},
            "filler1": {},
            "filler2": {},
            "filler3": {},
        }))
        result = isolate(
            ["bad", "library", "filler1", "filler2", "filler3"],
            self._oracle({"bad"}),
            closure=graph.closure,
            max_probes=60,
        )
        assert result.converged
        assert result.culprits == ("bad",)
        assert result.support == ("library",)
        assert "not implicated" in result.note

    def test_the_same_pack_without_a_graph_misreads_it_as_an_interaction(self, tmp_path):
        """The behaviour being corrected, pinned so it cannot come back.

        Launching exactly what is asked for means `{bad}` fails to run at all.
        The oracle reports that as INVALID rather than CLEAN, so the search
        declines to converge instead of naming a pair — which is the point: it
        does not silently produce the wrong answer.
        """
        def measure(mods, replicate):
            installed = set(mods)
            if "bad" in installed and "library" not in installed:
                return None  # will not launch
            return 16.7 * (1.35 if "bad" in installed else 1.0)

        result = isolate(
            ["bad", "library", "filler1", "filler2"],
            BenchmarkOracle(measure, runs_per_probe=5),
            max_probes=60,
        )
        assert result.culprits != ("bad",)
        assert result.invalid_probes > 0
        assert not result.minimal

    def test_an_unsatisfiable_subset_is_invalid_not_clean(self, tmp_path):
        graph = graph_from_metadata(pack(tmp_path, {
            "broken": {"depends": {"absent": "*"}},
            "fine": {},
        }))
        seen: list[tuple] = []

        def oracle(subset):
            seen.append(tuple(subset))
            return Outcome.CLEAN

        result = isolate(
            ["broken", "fine"], oracle, closure=graph.closure, verify=False,
        )
        invalid = [p for p in result.probes if p.outcome is Outcome.INVALID]
        assert invalid, "an unlaunchable subset must be recorded as invalid"
        assert all("broken" not in call for call in seen), (
            "an unsatisfiable subset must not reach the oracle at all"
        )

    def test_removing_a_needed_library_is_invalid_in_leave_one_out(self, tmp_path):
        """Not "this mod matters enormously" — the set simply will not load."""
        graph = graph_from_metadata(pack(tmp_path, {
            "consumer": {"depends": {"library": "*"}},
            "library": {},
        }))

        def closure(subset):
            # Restricted to the remaining set: the question is whether removing
            # a mod leaves something loadable, so re-adding it would answer a
            # different question.
            remaining = set(subset)
            missing = tuple(
                sorted(
                    dep
                    for mod in remaining
                    for dep in graph.dependencies_of(mod)
                    if graph.provided_by.get(dep) not in remaining
                )
            )
            from mcbench.diagnose import SubsetClosure

            return SubsetClosure(
                subset=tuple(subset), members=tuple(subset), missing=missing
            )

        outcomes = leave_one_out(
            ["consumer", "library"], lambda s: Outcome.CLEAN, closure=closure
        )
        assert outcomes["library"] is Outcome.INVALID
        assert outcomes["consumer"] is Outcome.CLEAN


class TestIntermittentFailures:
    def test_an_intermittent_launch_failure_does_not_convict(self):
        """Flaky launches are inconclusive, never clean and never a culprit.

        The subset that happens to fail on the runs it was given has not been
        shown to be slow, and the subset that happens to succeed has not been
        shown to be fast.
        """
        attempts = {"n": 0}

        def flaky(mods, replicate):
            attempts["n"] += 1
            # Roughly half of every arm's runs die, so no arm ever reaches the
            # floor and nothing can be concluded about any of them.
            if attempts["n"] % 2 == 0:
                return None
            return 16.7

        oracle = BenchmarkOracle(flaky, runs_per_probe=6)
        result = isolate(["a", "b", "c", "d"], oracle, max_probes=20)
        assert not result.converged
        assert "inconclusive" in result.note
        # Cost is the launches made, not the ones that produced a number.
        assert oracle.total_runs > oracle.successful_runs

    def test_a_failed_launch_still_counts_toward_the_cost(self):
        def half_broken(mods, replicate):
            return 16.7 if replicate < 3 else None

        oracle = BenchmarkOracle(half_broken, runs_per_probe=6)
        oracle(["a"])
        assert oracle.total_runs == 12
        assert oracle.successful_runs == 6
        assert oracle.failed_runs == 6
