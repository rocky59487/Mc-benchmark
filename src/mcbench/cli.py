"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, load_suite
from .metrics import METRICS, RunFlag, RunMetrics
from .planner import Cell, OrderStrategy, plan_runs
from .report import (
    SuiteResult,
    aggregate_cell,
    compare_to_baseline,
    render_json,
    render_markdown,
)
from .scenario import Category, ScenarioError, Side, load_scenarios, select

DEFAULT_SCENARIO_ROOT = "scenarios"


def _repo_root() -> Path:
    """Locate the repository root so scenarios resolve from anywhere."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "scenarios").is_dir() and (parent / "docs").is_dir():
            return parent
    return Path.cwd()


def _scenario_root(argument: str | None) -> Path:
    if argument:
        return Path(argument)
    candidate = Path(DEFAULT_SCENARIO_ROOT)
    return candidate if candidate.is_dir() else _repo_root() / DEFAULT_SCENARIO_ROOT


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_scenarios(args: argparse.Namespace) -> int:
    """List available scenarios."""
    scenarios = load_scenarios(_scenario_root(args.scenario_root))
    chosen = select(
        scenarios,
        side=Side(args.side) if args.side else None,
        category=Category(args.category) if args.category else None,
        tags=args.tag or None,
    )

    if not chosen:
        print("No scenarios matched.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([
            {
                "id": s.id, "version": s.version, "side": s.side.value,
                "category": s.category.value, "title": s.title,
                "primary_metric": s.primary_metric, "tick_warp": s.uses_tick_warp,
                "pool_key": s.pool_key,
            }
            for s in chosen
        ], indent=2))
        return 0

    width = max(len(s.id) for s in chosen)
    for scenario in chosen:
        warp = " [tick-warp]" if scenario.uses_tick_warp else ""
        print(
            f"{scenario.id:<{width}}  {scenario.side.value:<6} "
            f"{scenario.category.value:<15} {scenario.title}{warp}"
        )
    print(f"\n{len(chosen)} scenario(s).")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate scenarios and, if given, a suite manifest."""
    root = _scenario_root(args.scenario_root)
    scenarios = load_scenarios(root)
    print(f"✓ {len(scenarios)} scenario(s) valid in {root}")

    if not args.suite:
        return 0

    suite = load_suite(args.suite)
    print(f"✓ suite {suite.name!r} valid")
    print(f"  {suite.minecraft_version} / {suite.loader.value}")
    print(f"  {len(suite.variants)} variant(s), {len(suite.scenarios)} scenario(s)")
    print(f"  {suite.runs_per_cell} runs per cell, order={suite.order.value}")

    known = {s.id for s in scenarios}
    missing = [s for s in suite.scenarios if s not in known]
    if missing:
        print(
            f"✗ suite references unknown scenario(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    # A suite can be valid and still not admissible to the shared corpus. Say so
    # explicitly rather than letting an operator discover it after a long run.
    if suite.publishable:
        print("✓ publishable: results may enter the public corpus")
    else:
        print("⚠ not publishable:", file=sys.stderr)
        for reason in suite.unpublishable_reasons():
            print(f"    - {reason}", file=sys.stderr)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Show the execution schedule a suite would run."""
    suite = load_suite(args.suite)
    plan = plan_runs(
        suite.scenarios,
        [v.name for v in suite.variants],
        runs_per_cell=suite.runs_per_cell,
        strategy=suite.order,
        seed=suite.seed,
    )

    if args.json:
        print(json.dumps({
            "strategy": plan.strategy.value,
            "seed": plan.seed,
            "runs_per_cell": plan.runs_per_cell,
            "total_runs": len(plan),
            "runs": [asdict(r) | {"cell": str(r.cell)} for r in plan],
        }, indent=2, default=str))
        return 0

    print(f"Suite:    {suite.name}")
    print(f"Order:    {plan.strategy.value} (seed {plan.seed})")
    print(f"Runs:     {len(plan)} total, {plan.runs_per_cell} per cell")
    print(f"Cells:    {len(plan.cells)}")
    print()

    if plan.strategy is not OrderStrategy.INTERLEAVED:
        print(
            "⚠ Non-interleaved ordering confounds variant with wall-clock time, "
            "so thermal\n  drift and background load are attributed to the mods. "
            "Results are not publishable.\n",
            file=sys.stderr,
        )

    current: str | None = None
    for run in plan:
        if run.cell.scenario != current:
            current = run.cell.scenario
            print(f"\n{current}")
        print(f"  {run.position:>4}  round {run.round_index}  {run.cell.variant}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Resolve a suite's mods to concrete, hash-verified files."""
    from .config import Platform
    from .providers import ModrinthClient, ModrinthError

    suite = load_suite(args.suite)
    client = ModrinthClient(contact=args.contact)

    exit_code = 0
    for variant in suite.variants:
        if not variant.mods:
            print(f"{variant.name}: (baseline, no mods)")
            continue

        print(f"{variant.name}:")
        unsupported = [m for m in variant.mods if m.platform is not Platform.MODRINTH]
        if unsupported:
            # CurseForge resolution requires an operator-supplied key and cannot
            # cache; it is deliberately not wired into this path yet.
            print(
                f"  ! {len(unsupported)} mod(s) on non-Modrinth platforms are "
                f"not resolvable here; see docs/LICENSING.md",
                file=sys.stderr,
            )
            exit_code = 1

        try:
            resolved = client.resolve_all(
                [(m.project, m.version) for m in variant.mods
                 if m.platform is Platform.MODRINTH],
                game_version=suite.minecraft_version,
                loader=suite.loader.value,
            )
        except ModrinthError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            exit_code = 1
            continue

        for mod in resolved:
            print(f"  ✓ {mod}")
            print(f"      sha512 {mod.sha512[:32]}…")
            if args.download:
                path = client.download(mod)
                print(f"      cached {path}")
    return exit_code


def cmd_analyse(args: argparse.Namespace) -> int:
    """Aggregate recorded runs into a report.

    Input is a JSON document mapping ``"scenario/variant"`` to a list of runs,
    each run a mapping of metric key to value. This is the format the execution
    backend emits, and keeping analysis separate means results can be
    re-analysed — with a different ROPE, say — without re-running anything.
    """
    raw = json.loads(Path(args.results).read_text(encoding="utf-8"))
    suite_name = raw.get("suite", "mcbench results")
    baseline_name = raw.get("baseline")
    cells_raw = raw.get("cells", raw)

    if not baseline_name:
        print("✗ results must name a 'baseline' variant", file=sys.stderr)
        return 1

    cells: dict[Cell, list[RunMetrics]] = {}
    for key, runs in cells_raw.items():
        scenario, _, variant = key.partition("/")
        if not variant:
            print(f"✗ malformed cell key {key!r}; expected 'scenario/variant'",
                  file=sys.stderr)
            return 1
        cells[Cell(scenario, variant)] = [
            RunMetrics(
                values={k: float(v) for k, v in run.get("values", run).items()
                        if k in METRICS},
                flags=[RunFlag(f) for f in run.get("flags", [])]
                if isinstance(run, dict) else [],
            )
            for run in runs
        ]

    aggregated = {
        cell: aggregate_cell(cell, runs, seed=args.seed)
        for cell, runs in cells.items()
    }

    result = SuiteResult(
        suite_name=suite_name,
        baseline=baseline_name,
        cells=aggregated,
        provenance=raw.get("provenance", {}),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    scenarios = {c.scenario for c in aggregated}
    for scenario in sorted(scenarios):
        baseline_cell = aggregated.get(Cell(scenario, baseline_name))
        if baseline_cell is None:
            print(
                f"⚠ scenario {scenario!r} has no baseline cell; skipping comparisons",
                file=sys.stderr,
            )
            continue
        for cell, data in aggregated.items():
            if cell.scenario != scenario or cell.variant == baseline_name:
                continue
            result.comparisons.extend(
                compare_to_baseline(
                    baseline_cell, data, rope=args.rope, seed=args.seed
                )
            )

    output = render_json(result) if args.format == "json" else render_markdown(result)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(output)
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """List the metric registry."""
    width = max(len(k) for k in METRICS)
    for key, definition in METRICS.items():
        arrow = "↓ better" if definition.lower_is_better else "↑ better"
        print(f"{key:<{width}}  {arrow}  {definition.unit:<12} {definition.description}")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcbench",
        description="Controlled, reproducible performance benchmarking for Minecraft mods.",
        epilog="Methodology: docs/METHODOLOGY.md · Licensing: docs/LICENSING.md",
    )
    parser.add_argument("--version", action="version", version=f"mcbench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scenarios", help="list available scenarios")
    p.add_argument("--scenario-root")
    p.add_argument("--side", choices=[s.value for s in Side])
    p.add_argument("--category", choices=[c.value for c in Category])
    p.add_argument("--tag", action="append")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("validate", help="validate scenarios and a suite manifest")
    p.add_argument("--scenario-root")
    p.add_argument("--suite")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="show the execution schedule for a suite")
    p.add_argument("suite")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("resolve", help="resolve a suite's mods against Modrinth")
    p.add_argument("suite")
    p.add_argument("--contact", help="contact string for the User-Agent")
    p.add_argument("--download", action="store_true", help="download and verify files")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("analyse", aliases=["analyze"], help="aggregate runs into a report")
    p.add_argument("results")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--output", "-o")
    p.add_argument("--rope", type=float, default=0.02,
                   help="region of practical equivalence (default 0.02 = ±2%%)")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_analyse)

    p = sub.add_parser("metrics", help="list the metric registry")
    p.set_defaults(func=cmd_metrics)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ScenarioError, ConfigError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"✗ file not found: {exc.filename}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
