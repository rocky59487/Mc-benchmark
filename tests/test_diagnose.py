"""Tests for culprit isolation.

The oracle here is synthetic, which is the point: delta debugging is pure search
logic and can be verified exhaustively without launching a single game. The cases
that matter are the ones a naive bisection gets wrong.
"""

from __future__ import annotations

from mcbench.diagnose import (
    Outcome,
    isolate,
    leave_one_out,
)


def oracle_for(culprits: set[str], *, noisy_every: int = 0):
    """An oracle that reports a regression only when every culprit is present.

    Modelling the culprit as a *conjunction* is what makes interaction cases
    testable: with two culprits, no single mod and no half-set reproduces it.

    ``noisy_every`` makes every Nth *clean* probe come back inconclusive, which
    is how real noise behaves — a subset with no real effect sometimes fails to
    resolve. Probes that would report a regression stay decisive, so the search
    still has something true to follow.
    """
    calls: list[tuple[str, ...]] = []

    def oracle(subset):
        present = set(subset)
        calls.append(tuple(sorted(present)))
        if culprits <= present:
            return Outcome.REGRESSION
        if noisy_every and len(calls) % noisy_every == 0:
            return Outcome.INCONCLUSIVE
        return Outcome.CLEAN

    oracle.calls = calls  # type: ignore[attr-defined]
    return oracle


class TestSingleCulprit:
    def test_finds_one_bad_mod(self):
        mods = [f"mod{i}" for i in range(16)]
        result = isolate(mods, oracle_for({"mod7"}))
        assert result.converged
        assert result.culprits == ("mod7",)
        assert not result.is_interaction

    def test_search_is_far_cheaper_than_brute_force(self):
        """Each probe is several full game launches, so cost is the constraint."""
        mods = [f"mod{i}" for i in range(64)]
        result = isolate(mods, oracle_for({"mod50"}))
        assert result.culprits == ("mod50",)
        assert result.probe_count < 40, result.probe_count

    def test_handles_a_culprit_at_either_end(self):
        mods = [f"mod{i}" for i in range(20)]
        for target in ("mod0", "mod19"):
            result = isolate(mods, oracle_for({target}))
            assert result.culprits == (target,)

    def test_single_mod_set(self):
        result = isolate(["only"], oracle_for({"only"}))
        assert result.culprits == ("only",)


class TestInteractions:
    def test_finds_a_culprit_pair_a_bisection_would_miss(self):
        """The case plain bisection gets wrong.

        Neither mod regresses alone, so splitting them and testing halves finds
        nothing. ddmin's complement phase keeps them together and converges.
        """
        mods = [f"mod{i}" for i in range(16)]
        result = isolate(mods, oracle_for({"mod2", "mod11"}))
        assert result.converged
        assert set(result.culprits) == {"mod2", "mod11"}
        assert result.is_interaction

    def test_neither_member_reproduces_alone(self):
        oracle = oracle_for({"a", "b"})
        assert oracle(["a"]) is Outcome.CLEAN
        assert oracle(["b"]) is Outcome.CLEAN
        assert oracle(["a", "b"]) is Outcome.REGRESSION

    def test_finds_a_culprit_triple(self):
        mods = [f"mod{i}" for i in range(12)]
        result = isolate(mods, oracle_for({"mod1", "mod5", "mod9"}))
        assert result.converged
        assert set(result.culprits) == {"mod1", "mod5", "mod9"}

    def test_result_is_one_minimal(self):
        """Removing any single member must stop it reproducing."""
        mods = [f"mod{i}" for i in range(16)]
        culprits = {"mod3", "mod12"}
        result = isolate(mods, oracle_for(culprits))
        found = list(result.culprits)
        oracle = oracle_for(culprits)
        for member in found:
            reduced = [m for m in found if m != member]
            assert oracle(reduced) is not Outcome.REGRESSION


class TestNoRegression:
    def test_reports_nothing_to_isolate(self):
        # Without the verification probe the search would wander until the
        # budget ran out and report nothing useful.
        result = isolate(["a", "b", "c"], oracle_for({"absent"}))
        assert result.converged
        assert result.culprits == ()
        assert "nothing to isolate" in result.note

    def test_empty_mod_set(self):
        result = isolate([], oracle_for({"x"}))
        assert result.converged
        assert result.culprits == ()


class TestNoise:
    def test_inconclusive_is_never_treated_as_evidence(self):
        """An inconclusive probe must not be read as either answer.

        Counting it as a regression would frame an innocent mod; counting it
        silently as clean could hide the real one, so it is counted as
        not-reproducing *and reported*.
        """
        mods = [f"mod{i}" for i in range(8)]
        result = isolate(mods, oracle_for({"mod3"}, noisy_every=3))
        assert result.inconclusive_probes > 0
        assert "inconclusive" in result.note

    def test_still_converges_despite_some_noise(self):
        mods = [f"mod{i}" for i in range(12)]
        result = isolate(mods, oracle_for({"mod4"}, noisy_every=4))
        assert result.converged
        assert "mod4" in result.culprits

    def test_an_unconfirmed_baseline_is_not_reported_as_clean(self):
        """"We cannot tell" is not "there is nothing wrong".

        Reporting the latter would send someone away believing their pack is
        fine. Searching on regardless would be worse: every later probe would be
        measured against an effect that was never established.
        """
        def always_unsure(subset):
            return Outcome.INCONCLUSIVE

        result = isolate(["a", "b", "c"], always_unsure)
        assert not result.converged
        assert result.culprits == ()
        assert "could not be confirmed" in result.note
        assert "nothing to isolate" not in result.note


class TestBudget:
    def test_respects_the_probe_cap(self):
        # Each probe is a full benchmark cell, so an unbounded search could run
        # for days.
        mods = [f"mod{i}" for i in range(64)]
        result = isolate(mods, oracle_for({"mod30", "mod31"}), max_probes=5)
        assert not result.converged
        assert result.probe_count <= 5
        assert "max_probes" in result.note

    def test_caches_repeated_subsets(self):
        oracle = oracle_for({"mod5"})
        mods = [f"mod{i}" for i in range(16)]
        result = isolate(mods, oracle)
        # The recorded probes are the distinct ones; a cache hit must not be
        # billed as another game launch.
        keys = [tuple(sorted(p.subset)) for p in result.probes]
        assert len(keys) == len(set(keys))


class TestLeaveOneOut:
    def test_costs_exactly_one_probe_per_mod(self):
        calls: list[int] = []

        def oracle(subset):
            calls.append(len(subset))
            return Outcome.REGRESSION if "bad" in subset else Outcome.CLEAN

        mods = ["a", "b", "bad", "c"]
        outcomes = leave_one_out(mods, oracle)
        assert len(calls) == len(mods)
        assert outcomes["bad"] is Outcome.CLEAN
        assert outcomes["a"] is Outcome.REGRESSION

    def test_an_interaction_shows_as_no_single_mod_helping(self):
        """The signal that says "use isolate() instead".

        With a culprit pair, removing either member clears it, so leave-one-out
        implicates both — which is correct but not minimal.
        """
        oracle = oracle_for({"x", "y"})
        outcomes = leave_one_out(["x", "y", "z"], oracle)
        assert outcomes["x"] is Outcome.CLEAN
        assert outcomes["y"] is Outcome.CLEAN
        assert outcomes["z"] is Outcome.REGRESSION


class TestSummary:
    def test_reports_an_interaction_distinctly(self):
        mods = [f"mod{i}" for i in range(8)]
        result = isolate(mods, oracle_for({"mod1", "mod6"}))
        assert "together" in result.summary()

    def test_reports_a_single_culprit_plainly(self):
        result = isolate([f"mod{i}" for i in range(8)], oracle_for({"mod2"}))
        assert result.summary().startswith("mod2 causes")
