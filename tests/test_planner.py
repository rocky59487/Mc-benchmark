"""Tests for run planning.

The interleaving guarantee is the one property here that actually protects a
result from being wrong, so it is tested directly rather than through the shape
of the output.
"""

from __future__ import annotations

import pytest

from mcbench.planner import (
    Cell,
    OrderStrategy,
    factorial_variants,
    plan_runs,
)


def _slot_spread(runs) -> float:
    """Difference between the highest and lowest mean slot within a round.

    Slot rather than global position: position also counts whole rounds, which
    every variant gets equally, and the difference that matters is where inside
    a round a variant tends to land. A machine warms through a round.
    """
    by_round: dict[int, list[str]] = {}
    for run in runs:
        by_round.setdefault(run.round_index, []).append(run.cell.variant)

    slots: dict[str, list[int]] = {}
    for names in by_round.values():
        for slot, name in enumerate(names):
            slots.setdefault(name, []).append(slot)

    means = [sum(v) / len(v) for v in slots.values()]
    return max(means) - min(means)


class TestPlanShape:
    def test_produces_one_run_per_cell_per_replicate(self):
        plan = plan_runs(["s1", "s2"], ["a", "b", "c"], runs_per_cell=5)
        assert len(plan) == 2 * 3 * 5
        for scenario in ("s1", "s2"):
            for variant in ("a", "b", "c"):
                assert len(plan.for_cell(Cell(scenario, variant))) == 5

    def test_positions_are_a_contiguous_sequence(self):
        plan = plan_runs(["s1"], ["a", "b"], runs_per_cell=5)
        assert [r.position for r in plan] == list(range(len(plan)))

    def test_is_reproducible_for_a_given_seed(self):
        first = plan_runs(["s1"], ["a", "b", "c"], runs_per_cell=5, seed=99)
        second = plan_runs(["s1"], ["a", "b", "c"], runs_per_cell=5, seed=99)
        assert [(r.cell, r.position) for r in first] == [
            (r.cell, r.position) for r in second
        ]

    def test_different_seeds_give_different_orders(self):
        first = plan_runs(["s1"], ["a", "b", "c"], runs_per_cell=7, seed=1)
        second = plan_runs(["s1"], ["a", "b", "c"], runs_per_cell=7, seed=2)
        assert [r.cell.variant for r in first] != [r.cell.variant for r in second]


class TestInterleaving:
    def test_every_round_contains_every_variant_exactly_once(self):
        variants = ["a", "b", "c", "d"]
        plan = plan_runs(["s1"], variants, runs_per_cell=7, seed=5)
        for round_index in range(7):
            in_round = [
                r.cell.variant for r in plan if r.round_index == round_index
            ]
            assert sorted(in_round) == sorted(variants)

    def test_mean_position_is_similar_across_variants(self):
        """The property that makes interleaving work.

        If one variant systematically ran earlier than another, it would enjoy a
        cooler machine and a quieter system, and that advantage would reproduce
        across repeats — indistinguishable from a real improvement. Interleaving
        keeps the mean execution position close for every variant.
        """
        variants = ["a", "b", "c"]
        plan = plan_runs(["s1"], variants, runs_per_cell=21, seed=7)
        positions = plan.positions_by_variant()
        means = [sum(p) / len(p) for p in positions.values()]
        spread = max(means) - min(means)
        # With a shuffled round-robin the spread is a small fraction of the run.
        assert spread < len(plan) * 0.10

    def test_the_bound_holds_at_the_run_counts_suites_use(self):
        """Twenty-one rounds is not what anyone runs.

        The test above passes on independent shuffling too, because at that
        many rounds the averaging does the work. A suite runs five. This is the
        same property at a size someone will actually use, and it fails on the
        implementation this replaced.
        """
        for count in (5, 6, 7, 8):
            for names in (["a", "b", "c"], list("abcdef"), list("abcdefgh")):
                bound = (len(names) - 1) / count if count % 2 else 0.0
                for seed in range(60):
                    spread = _slot_spread(
                        plan_runs(["s1"], names, runs_per_cell=count, seed=seed)
                    )
                    assert spread <= bound + 1e-9, (
                        f"{len(names)} variants, {count} rounds, seed {seed}: "
                        f"{spread} > {bound}"
                    )

    def test_an_even_round_count_balances_exactly(self):
        # Worth stating on its own: it is the reason to prefer an even
        # runs_per_cell, and it costs one run per cell over the odd number
        # below it.
        for seed in range(40):
            plan = plan_runs(["s1"], list("abcdef"), runs_per_cell=6, seed=seed)
            assert _slot_spread(plan) == 0.0

    def test_balance_holds_within_each_scenario(self):
        # Scenarios run end to end, so each is its own stretch of wall clock
        # and needs balancing on its own. A schedule balanced only over the
        # concatenation could be skewed one way in the first and the other way
        # in the second, and cancel on paper.
        plan = plan_runs(["s1", "s2"], list("abcdef"), runs_per_cell=6, seed=11)
        for scenario in ("s1", "s2"):
            runs = [r for r in plan if r.cell.scenario == scenario]
            assert _slot_spread(runs) == 0.0

    def test_blocked_ordering_shows_the_bias_interleaving_removes(self):
        """Demonstrates why blocked ordering is not admissible.

        Under blocking each variant occupies a contiguous span, so mean
        positions are maximally separated — exactly the confound between variant
        and wall-clock time that interleaving exists to break.
        """
        variants = ["a", "b", "c"]
        plan = plan_runs(
            ["s1"], variants, runs_per_cell=21,
            strategy=OrderStrategy.BLOCKED, seed=7,
        )
        means = [sum(p) / len(p) for p in plan.positions_by_variant().values()]
        spread = max(means) - min(means)
        assert spread > len(plan) * 0.50

    def test_round_order_actually_varies_between_rounds(self):
        # A fixed round-robin (always a,b,c) would still give each variant a
        # constant offset within every round; shuffling removes that too.
        plan = plan_runs(["s1"], ["a", "b", "c"], runs_per_cell=12, seed=3)
        orders = {
            tuple(r.cell.variant for r in plan if r.round_index == i)
            for i in range(12)
        }
        assert len(orders) > 1


class TestValidation:
    def test_rejects_too_few_runs_per_cell(self):
        with pytest.raises(ValueError, match="at least 5"):
            plan_runs(["s1"], ["a"], runs_per_cell=3)

    def test_rejects_duplicate_variants(self):
        with pytest.raises(ValueError, match="unique"):
            plan_runs(["s1"], ["a", "a"], runs_per_cell=5)

    def test_rejects_empty_inputs(self):
        with pytest.raises(ValueError, match="at least one scenario"):
            plan_runs([], ["a"], runs_per_cell=5)
        with pytest.raises(ValueError, match="at least one variant"):
            plan_runs(["s1"], [], runs_per_cell=5)


class TestFactorial:
    def test_two_factors_give_the_four_interaction_cells(self):
        combos = factorial_variants(["sodium", "lithium"])
        assert combos == [
            (),
            ("sodium",),
            ("lithium",),
            ("sodium", "lithium"),
        ]

    def test_baseline_is_first(self):
        assert factorial_variants(["a", "b", "c"])[0] == ()

    def test_size_is_two_to_the_n(self):
        assert len(factorial_variants(["a", "b", "c"])) == 8
        assert len(factorial_variants(["a", "b", "c", "d"])) == 16

    def test_caps_the_design_to_keep_run_counts_sane(self):
        with pytest.raises(ValueError, match="capped at 4"):
            factorial_variants(["a", "b", "c", "d", "e"])

    def test_rejects_duplicate_factors(self):
        with pytest.raises(ValueError, match="unique"):
            factorial_variants(["a", "a"])
