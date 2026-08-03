"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import ConfigError, Loader, load_suite
from .metrics import METRICS, RunFlag, RunMetrics
from .planner import Cell, OrderStrategy, plan_runs
from .report import (
    DEFAULT_FDR,
    REFERENCE_SCENARIO,
    ResultsError,
    SuiteResult,
    aggregate_cell,
    apply_fdr,
    compare_to_baseline,
    parse_results_document,
    reference_ratios,
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
    # Results are untrusted input — they are meant to be exchanged, and a corpus
    # would ingest them from strangers. Validated before anything reaches the
    # statistics, because a non-finite value poisons everything downstream.
    suite_name, baseline_name, cells_raw = parse_results_document(raw)

    # Failed attempts are separated here rather than dropped. They carry no
    # values and so contribute nothing to any estimate, but they are counted, so
    # that a cell of five values out of twelve attempts cannot serialise as
    # though five were all that were ever planned.
    cells: dict[Cell, list[RunMetrics]] = {}
    attempts: dict[Cell, int] = {}
    failures: dict[Cell, int] = {}
    for key, runs in cells_raw.items():
        scenario, _, variant = key.partition("/")
        cell = Cell(scenario, variant)
        attempts[cell] = len(runs)
        failures[cell] = sum(1 for run in runs if run.get("status") == "failed")
        cells[cell] = [
            RunMetrics(
                values={k: v for k, v in run["values"].items() if k in METRICS},
                flags=[RunFlag(f) for f in run["flags"] if f in set(RunFlag)],
            )
            for run in runs
            if run.get("status") != "failed"
        ]

    aggregated = {
        cell: aggregate_cell(
            cell, runs, seed=args.seed,
            attempted=attempts[cell], failed=failures[cell],
        )
        for cell, runs in cells.items()
    }

    result = SuiteResult(
        suite_name=suite_name,
        baseline=baseline_name,
        cells=aggregated,
        provenance=raw.get("provenance", {}),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
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

    # Multiplicity control, interaction terms, and reference normalisation are
    # applied here, on the ordinary analysis path. Each of these was implemented
    # in the library and documented as part of the method while no normal run
    # produced it, which is worse than not having them: a reader who knows the
    # methodology assumes the protection was applied.
    result.multiplicity = apply_fdr(result.comparisons, fdr=args.fdr)
    result.interactions = _build_interactions(
        aggregated,
        groups=_interaction_groups(raw, args.suite),
        baseline=baseline_name,
        rope=args.rope,
        seed=args.seed,
    )
    result.normalised = reference_ratios(
        aggregated, baseline=baseline_name,
        reference_scenario=args.reference_scenario,
    )

    # Order effects are plotted from the execution positions recorded at run
    # time, so a reader can audit whether interleaving actually held.
    order_points: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for key, runs in cells_raw.items():
        scenario, _, variant = key.partition("/")
        for run in runs:
            if not isinstance(run, dict) or "position" not in run:
                continue
            values = run.get("values", {})
            metric = next(
                (m for m in ("frametime_mean_ms", "mspt_mean") if m in values), None
            )
            if metric:
                order_points.setdefault(scenario, {}).setdefault(variant, []).append(
                    (int(run["position"]), float(values[metric]))
                )

    if args.export_dir:
        from .export import render_html_report, tables_for
        from .export.tables import write_all

        export_dir = Path(args.export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        tables = tables_for(result, raw_runs=cells_raw)

        # `--table-format` is repeatable and so defaults to None, which would be
        # passed straight through and shadow write_all's own default.
        written = write_all(
            tables, str(export_dir), formats=args.table_format or ["csv"]
        )
        (export_dir / "report.json").write_text(render_json(result), encoding="utf-8")
        (export_dir / "report.md").write_text(render_markdown(result), encoding="utf-8")
        html_path = export_dir / "report.html"
        html_path.write_text(
            render_html_report(
                result, raw_runs=cells_raw, order_points=order_points,
                preflight=raw.get("preflight"), tables=tables,
            ),
            encoding="utf-8",
        )
        for path in [*written, str(export_dir / "report.json"),
                     str(export_dir / "report.md"), str(html_path)]:
            print(f"Wrote {path}")
        return 0

    output = render_json(result) if args.format == "json" else render_markdown(result)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(output)
    return 0


def _interaction_groups(
    raw: dict, suite_path: str | None
) -> list[tuple[str, str, str]]:
    """Which mod pairs to compute an interaction term for.

    Taken from the results document when the run recorded them, and from a suite
    manifest when the operator points at one. Re-analysis is a first-class
    operation here — the whole reason analysis is a separate command — so the
    declaration has to survive in the results document rather than living only
    in a manifest that may not be to hand.
    """
    groups: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(entry) -> None:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ResultsError(
                f"interaction group {entry!r} must name exactly three variants: "
                f"[A, B, A+B]. The fourth cell is the suite's baseline."
            )
        key = tuple(str(name) for name in entry)
        if key not in seen:
            seen.add(key)
            groups.append(key)

    for entry in raw.get("interactions") or []:
        add(entry)

    if suite_path:
        for entry in load_suite(suite_path).interactions:
            add(list(entry))

    return groups


def _build_interactions(
    cells: dict[Cell, object],
    *,
    groups: Sequence[tuple[str, str, str]],
    baseline: str,
    rope: float,
    seed: int,
) -> list[dict]:
    """Build every declared interaction contrast that has four populated cells."""
    from .report import build_interaction

    out: list[dict] = []
    for scenario in sorted({c.scenario for c in cells}):
        by_variant = {
            c.variant: data for c, data in cells.items() if c.scenario == scenario
        }
        if baseline not in by_variant:
            continue
        metrics = sorted(by_variant[baseline].samples)
        for index, (a, b, ab) in enumerate(groups):
            if not {a, b, ab} <= set(by_variant):
                continue
            for offset, metric in enumerate(metrics):
                item = build_interaction(
                    scenario, metric, by_variant,
                    none_key=baseline, a_key=a, b_key=b, ab_key=ab,
                    rope=rope, seed=seed + index * 101 + offset * 7,
                )
                if item is not None:
                    out.append(item)
    return out


def cmd_world(args: argparse.Namespace) -> int:
    """Fingerprint one or more saved worlds, and say whether they agree.

    The check METHODOLOGY §7 relies on, exposed so it can be run by hand. Two
    worlds that were supposed to be identical and are not is a finding on its
    own — usually a mod that alters worldgen, occasionally a scenario whose
    setup is not as deterministic as it looks.
    """
    from .world import WorldError, fingerprint_world

    results = []
    for path in args.world:
        try:
            result = fingerprint_world(path, dimension=args.dimension)
        except (WorldError, OSError) as exc:
            print(f"✗ {path}: {exc}", file=sys.stderr)
            return 2
        results.append((path, result))

    if args.json:
        print(json.dumps([
            {
                "world": path, "sha256": r.sha256, "chunks": r.chunks,
                "regions": r.regions, "complete": r.complete,
                "unreadable": r.unreadable,
            }
            for path, r in results
        ], indent=2))
        return 0

    width = max(len(path) for path, _ in results)
    for path, result in results:
        mark = "✓" if result.complete else "⚠"
        print(f"{mark} {path:<{width}}  {result.sha256}")
        print(f"  {' ' * width}  {result.chunks} chunks, {result.regions} regions")
        for problem in result.unreadable:
            print(f"  {' ' * width}  ! {problem}", file=sys.stderr)

    distinct = {r.sha256 for _, r in results if r.complete}
    if len(results) > 1:
        print()
        if len(distinct) <= 1:
            print("✓ all worlds identical; runs over them may be pooled")
        else:
            print(
                f"✗ {len(distinct)} distinct worlds; runs over them must not be "
                f"pooled (METHODOLOGY §7)",
                file=sys.stderr,
            )
            return 1
    # An incomplete read is not a pass: it is a fingerprint over whatever
    # happened to be readable, which two broken worlds could easily share.
    return 0 if all(r.complete for _, r in results) else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    """Static health check of a mod set: conflicts, gaps, and mixin contention.

    Runs on jars alone — no game, no account, no GPU. That is the point: the
    fastest useful answer about a modpack is the one you get before spending two
    hours benchmarking a pack that was never going to load.
    """
    from .inspect import InspectionTarget, inspect_mods, read_jars
    from .inspect import Severity as InspectSeverity

    paths: list[Path] = []
    missing: list[str] = []
    for entry in args.paths:
        path = Path(entry)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.jar")))
        elif path.is_file():
            paths.append(path)
        else:
            missing.append(entry)

    if missing:
        # Named and absent. Silently inspecting the rest would report on a
        # different pack than the operator asked about.
        print(f"✗ not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not paths:
        print("✗ no jars found", file=sys.stderr)
        return 2

    target = InspectionTarget(
        loader=args.loader or "",
        minecraft_version=args.minecraft_version or "",
        loader_version=args.loader_version or "",
        provisioned=frozenset(args.provisioned or ()),
    )

    mods = read_jars(paths, scan_mixins=not args.no_mixins)
    result = inspect_mods(
        mods, overlap_threshold=args.overlap_threshold, target=target
    )

    if args.json:
        print(json.dumps({
            "target": {
                "loader": target.loader,
                "minecraft_version": target.minecraft_version,
                "loader_version": target.loader_version,
                "provisioned": sorted(target.provisioned),
                "range_dialect": target.dialect.value,
            },
            "mods": [
                {
                    "id": m.mod_id, "version": m.version, "loader": m.loader,
                    "environment": m.environment, "license": m.license,
                    "file": m.path.name,
                    "depends": m.depends, "breaks": m.breaks,
                    "recommends": m.recommends,
                    "provides": list(m.provides),
                    "mixin_configs": list(m.mixin_configs),
                    "mixin_targets": sorted(m.mixin_targets),
                    "verified_mixin_targets": sorted(m.verified_targets),
                    "nested_jars": list(m.nested_jars),
                    "nested": [
                        {"id": n.mod_id, "version": n.version,
                         "provides": list(n.provides)}
                        for n in m.nested
                    ],
                    "unreadable": m.unreadable,
                    "errors": list(m.errors),
                }
                for m in result.mods
            ],
            # Errors and warnings are separated in the structured output, not
            # merely tagged. A consumer deciding whether to proceed should not
            # have to filter a flat list correctly to avoid running a pack that
            # cannot load.
            "errors": [_finding_json(f) for f in result.errors],
            "warnings": [_finding_json(f) for f in result.warnings],
            "findings": [_finding_json(f) for f in result.findings],
            "overlaps": {k: list(v) for k, v in result.overlaps.items()},
            "verified_overlaps": {
                k: list(v) for k, v in result.verified_overlaps.items()
            },
            "ok": result.ok,
        }, indent=2))
        return 0 if result.ok else 1

    width = max(len(m.mod_id) for m in result.mods)
    print(f"{len(result.mods)} mod(s):")
    for mod in result.mods:
        env = "" if mod.environment == "*" else f" [{mod.environment}]"
        mixins = f"  {len(mod.mixin_targets)} mixin targets" if mod.mixin_targets else ""
        print(f"  {mod.mod_id:<{width}}  {mod.version:<22} {mod.loader}{env}{mixins}")

    mark = {
        InspectSeverity.ERROR: "✗", InspectSeverity.WARNING: "⚠",
        InspectSeverity.INFO: "·",
    }
    if result.findings:
        print()
        for finding in result.findings:
            print(f"{mark[finding.severity]} {finding.summary}")
            if finding.detail:
                for line in _wrap(finding.detail, 2):
                    print(line)

    verified = bool(result.verified_overlaps)
    hotspots = result.hotspots(limit=args.top, verified_only=verified)
    if hotspots:
        source = (
            "declared @Mixin targets" if verified
            else "mixin constant-pool references — weaker evidence"
        )
        print(f"\nMost contended classes, from {source}:")
        for contended, owners in hotspots:
            print(f"  {len(owners)}x  {contended}")
            print(f"        {', '.join(owners)}")

    print()
    if result.ok:
        if result.warnings:
            print(f"✓ no blocking problems found "
                  f"({len(result.warnings)} warning(s) above)")
        else:
            print("✓ no blocking problems found")
    else:
        print(f"✗ {len(result.errors)} blocking problem(s)")
    return 0 if result.ok else 1


def _finding_json(finding) -> dict:
    return {
        "severity": finding.severity.value,
        "code": finding.code,
        "summary": finding.summary,
        "detail": finding.detail,
        "mods": list(finding.mods),
    }


def cmd_targets(args: argparse.Namespace) -> int:
    """Show which scenarios compile for which targets.

    The point of the target layer is that one scenario definition serves every
    platform. This makes that claim checkable instead of asserted, and it is how
    you find out that a scenario silently needs Carpet before a suite spends two
    hours discovering it.
    """
    from .runner.plan import check_target
    from .targets import Dialect, Target, UnsupportedTarget

    scenarios = load_scenarios(_scenario_root(args.scenario_root))
    specs = args.target or [
        "fabric:1.21.1", "neoforge:1.21.1", "forge:1.21.1",
        "paper:1.21.1", "paper:1.20.4",
    ]
    mods = args.with_mod or []

    try:
        targets = [Target.parse(spec, mods=mods) for spec in specs]
    except UnsupportedTarget as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    rows = []
    for scenario in scenarios:
        row = {"scenario": scenario.id, "results": {}}
        for target in targets:
            missing = check_target(scenario, target)
            row["results"][str(target)] = {
                "ok": not missing,
                "missing": [c.value for c in missing],
                "reasons": [Dialect(target).explain(c) for c in missing],
            }
        rows.append(row)

    if args.json:
        print(json.dumps({
            "targets": [str(t) for t in targets],
            "mods": mods,
            "scenarios": rows,
        }, indent=2))
        return 0

    width = max(len(r["scenario"]) for r in rows)
    headers = [str(t) for t in targets]
    column = max(max((len(h) for h in headers), default=8), 8)

    print(" " * width + "  " + "  ".join(h.ljust(column) for h in headers))
    for row in rows:
        cells = []
        for header in headers:
            result = row["results"][header]
            cells.append(("✓" if result["ok"] else "✗").ljust(column))
        print(f"{row['scenario']:<{width}}  " + "  ".join(cells))

    print()
    if mods:
        print(f"Assuming these mods present: {', '.join(mods)}")
    for header in headers:
        blocked = [r for r in rows if not r["results"][header]["ok"]]
        if not blocked:
            print(f"{header}: all {len(rows)} scenarios runnable")
            continue
        print(f"{header}: {len(rows) - len(blocked)}/{len(rows)} runnable")
        seen: set[str] = set()
        for row in blocked:
            for reason in row["results"][header]["reasons"]:
                if reason not in seen:
                    seen.add(reason)
                    print(f"    - {reason}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check whether this machine can produce a publishable measurement."""
    from .runner import Severity, run_preflight

    needs_gpu = args.side != "server"
    result = run_preflight(
        needs_gpu=needs_gpu,
        heap_mb=args.heap_mb,
        require_account=not args.no_account_check,
    )

    if args.json:
        print(json.dumps({
            "admissible": result.admissible,
            "publishable": result.publishable,
            "host": result.host,
            "checks": [
                {"name": c.name, "severity": c.severity.value,
                 "detail": c.detail, "remedy": c.remedy}
                for c in result.checks
            ],
        }, indent=2))
        return 0 if result.admissible else 1

    mark = {
        Severity.OK: "✓", Severity.INFO: "·",
        Severity.WARN: "⚠", Severity.BLOCK: "✗",
    }
    width = max(len(c.name) for c in result.checks)
    for check in result.checks:
        print(f"{mark[check.severity]} {check.name:<{width}}  {check.detail}")
        if check.remedy:
            for line in _wrap(check.remedy, width + 4):
                print(line)

    print()
    if result.admissible and result.publishable:
        print("✓ this machine can produce publishable results")
    elif result.admissible:
        print("⚠ measurement can proceed, but results are not publishable:")
        for check in result.warnings:
            print(f"    - {check.name}: {check.detail}")
    else:
        print("✗ measurement must not proceed:")
        for check in result.blockers:
            print(f"    - {check.name}: {check.detail}")
    return 0 if result.admissible else 1


def _wrap(text: str, indent: int, width: int = 78) -> list[str]:
    import textwrap

    return textwrap.wrap(
        text, width=width,
        initial_indent=" " * indent, subsequent_indent=" " * indent,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a suite headlessly and write the results."""
    from .runner import Harness, HarnessError
    from .runner.harness import outcomes_to_cells, run_counts

    suite = load_suite(args.suite)
    scenarios = {s.id: s for s in load_scenarios(_scenario_root(args.scenario_root))}

    missing = [s for s in suite.scenarios if s not in scenarios]
    if missing:
        print(f"✗ unknown scenario(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    def emit(event: str, payload: dict) -> None:
        if args.json_events:
            print(json.dumps({"event": event, **payload}), flush=True)
        elif event == "run.start":
            print(f"  [{payload['position']:>4}] {payload['cell']} …",
                  end="", flush=True)
        elif event == "run.done":
            flags = f" ({', '.join(payload['flags'])})" if payload["flags"] else ""
            print(f" {payload['wall_clock_s']}s{flags}", flush=True)
        elif event == "run.fail":
            print(f" FAILED: {payload['error']}", flush=True)
        elif event == "suite.start":
            print(f"Executing {payload['runs']} runs "
                  f"({payload['strategy']}, seed {payload['seed']})")

    harness = Harness(
        suite, scenarios,
        work_dir=args.work_dir,
        headlessmc=args.headlessmc,
        local_root=args.local_root,
        agent_jar=args.agent_jar,
        probe_jar=args.probe_jar,
        fabric_api_jar=args.fabric_api_jar,
        on_event=emit,
    )

    preflight = harness.preflight(require_account=not args.no_account_check)
    preflight.forced = args.force and not preflight.admissible
    if not preflight.admissible and not args.force:
        print("✗ preflight failed; refusing to run:", file=sys.stderr)
        for check in preflight.blockers:
            print(f"    {check.name}: {check.detail}", file=sys.stderr)
            if check.remedy:
                for line in _wrap(check.remedy, 6):
                    print(line, file=sys.stderr)
        print("\n  Use --force to override. Results from a forced run are "
              "flagged and must not be published.", file=sys.stderr)
        return 1

    if preflight.warnings:
        print("⚠ preflight warnings (results will be flagged):", file=sys.stderr)
        for check in preflight.warnings:
            print(f"    {check.name}: {check.detail}", file=sys.stderr)

    try:
        harness.resolve_all()
        outcomes = harness.run_suite(
            stop_on_failure=args.stop_on_failure, timeout_s=args.timeout
        )
    except HarnessError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    counts = run_counts(outcomes)
    payload = {
        "suite": suite.name,
        "baseline": suite.baseline,
        "provenance": harness.provenance(preflight),
        # Carried so that re-analysis can compute the interaction terms without
        # the manifest to hand; analysis is a separate command precisely so a
        # result can be re-read later, and a declaration that lived only in the
        # suite file would not survive that.
        "interactions": [list(group) for group in suite.interactions],
        "runs": counts,
        "preflight": preflight.to_dict(),
        "cells": outcomes_to_cells(outcomes),
    }
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    attempted = len(outcomes)
    completed = sum(1 for o in outcomes if o.metrics is not None)
    admissible = sum(1 for o in outcomes if o.succeeded)
    print(f"\n{attempted} run(s) attempted → {args.output}")
    print(f"  {completed} produced measurements, {admissible} admissible")
    if admissible < attempted:
        print(f"  {attempted - completed} failed to produce any measurement; "
              f"every attempt is recorded in the results document with its "
              f"status, error, and log path.")
    return 0 if admissible else 1


def _dependency_graph(jars_by_id: dict, candidates: Sequence[str]):
    """Build the bisect's dependency graph by reading the resolved jars.

    Metadata that cannot be read yields no edges for that mod rather than an
    error. A jar whose descriptor is unreadable is a real problem, but it is
    ``mcbench inspect``'s problem to report; refusing to bisect over it would
    turn a diagnostic tool off exactly when something is wrong.
    """
    from .deps import graph_from_metadata
    from .inspect import read_jars

    mods = read_jars(sorted(set(jars_by_id.values())), scan_mixins=False)
    # inspect keys on the id a jar declares; the bisect keys on the id the suite
    # used. Reconciled here so a project slug that differs from the mod id does
    # not silently produce a graph with no edges at all.
    by_path = {mod.path: mod for mod in mods}
    for project, jar in jars_by_id.items():
        found = by_path.get(jar)
        if found is not None and found.mod_id != project:
            found.provides = (*found.provides, found.mod_id)
            found.mod_id = project
    return graph_from_metadata(mods, candidates=candidates)


def cmd_bisect(args: argparse.Namespace) -> int:
    """Isolate which mod, or which combination, causes a regression."""
    from .diagnose import Outcome, isolate
    from .runner import Harness, HarnessError
    from .runner.oracle import BenchmarkOracle

    suite = load_suite(args.suite)
    scenarios = {s.id: s for s in load_scenarios(_scenario_root(args.scenario_root))}

    scenario_id = args.scenario or (suite.scenarios[0] if suite.scenarios else None)
    if scenario_id not in scenarios:
        print(f"✗ unknown scenario {scenario_id!r}", file=sys.stderr)
        return 2

    harness = Harness(
        suite, scenarios,
        work_dir=args.work_dir,
        headlessmc=args.headlessmc,
        local_root=args.local_root,
        agent_jar=args.agent_jar,
        probe_jar=args.probe_jar,
        fabric_api_jar=args.fabric_api_jar,
    )

    preflight = harness.preflight(require_account=not args.no_account_check)
    preflight.forced = args.force and not preflight.admissible
    if not preflight.admissible and not args.force:
        print("✗ preflight failed; refusing to bisect:", file=sys.stderr)
        for check in preflight.blockers:
            print(f"    {check.name}: {check.detail}", file=sys.stderr)
        return 1

    try:
        resolved = harness.resolve_all()
    except HarnessError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    # Every mod across every variant becomes a bisect candidate; a pack is the
    # union of what the suite declares.
    jars_by_id: dict[str, Path] = {}
    for variant in suite.variants:
        entry = resolved.get(variant.name)
        if entry is None:
            continue
        # strict: a length mismatch would silently map a mod id onto another
        # mod's jar, and the bisect would then blame the wrong mod.
        for mod, jar in zip(variant.mods, entry.jars, strict=True):
            jars_by_id[mod.project] = jar

    candidates = args.mod or sorted(jars_by_id)
    unknown = [m for m in candidates if m not in jars_by_id]
    if unknown:
        print(f"✗ not in this suite: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not candidates:
        print("✗ no mods to bisect", file=sys.stderr)
        return 2

    # Read the jars so the search knows which mods need which. Without this a
    # subset is launched exactly as asked, and a culprit that needs a library
    # fails to launch on its own while the library alone fails to reproduce —
    # from which the search concludes the two interact, and two mod authors get
    # a bug report about a conflict that does not exist.
    graph = _dependency_graph(jars_by_id, candidates)
    unlaunchable = sorted(graph.unresolved)
    if unlaunchable:
        print(
            f"⚠ {len(unlaunchable)} mod(s) declare dependencies nothing in this "
            f"pack provides; subsets containing them are reported invalid rather "
            f"than clean: {', '.join(unlaunchable)}\n",
            file=sys.stderr,
        )

    def measure(mods, replicate):
        return harness.measure_subset(
            scenario_id, mods, replicate,
            metric=args.metric, jars_by_id=jars_by_id, timeout_s=args.timeout,
        )

    oracle = BenchmarkOracle(
        measure,
        metric=args.metric,
        runs_per_probe=args.runs_per_probe,
        rope=args.rope,
        seed=suite.seed,
        interleave_baseline=not args.reuse_baseline,
    )

    if args.reuse_baseline:
        print(
            "⚠ --reuse-baseline measures the baseline once and compares every "
            "later probe against it.\n"
            "  Over a long search that is blocked ordering: drift between the "
            "baseline and a late\n  probe gets attributed to a mod. Exploratory "
            "use only.\n",
            file=sys.stderr,
        )

    print(f"Bisecting {len(candidates)} mod(s) on {scenario_id}")
    print(f"Metric: {args.metric}  ROPE: ±{args.rope * 100:g}%  "
          f"{args.runs_per_probe} runs per arm per probe\n")

    result = isolate(
        candidates, oracle, max_probes=args.max_probes, closure=graph.closure
    )

    print()
    # Probes that were never launched — an unsatisfiable subset — have no oracle
    # record, so the two lists are matched by walking the records in order
    # rather than zipped positionally.
    records = iter(oracle.records)
    for probe in result.probes:
        label = "+".join(probe.subset) or "baseline"
        if len(label) > 60:
            label = label[:57] + "…"
        if probe.outcome is Outcome.INVALID:
            note = probe.detail
        else:
            record = next(records, None)
            note = record.note if record is not None else ""
        support = f" (+{len(probe.support)} dep)" if probe.support else ""
        print(f"  {probe.outcome.value:<13} {note or ''}  [{label}]{support}")

    print()
    print(result.summary())
    print(
        f"Cost: {result.probe_count} probes, {oracle.total_runs} game launches "
        f"({oracle.failed_runs} of which failed)"
    )

    if result.support:
        print(
            f"\nInstalled to satisfy dependencies, not implicated: "
            f"{', '.join(result.support)}. The search never had the option of "
            f"testing the set without them, so they are not ruled out either."
        )
    if result.is_interaction:
        print(
            "\nThis is an interaction: no single mod reproduces it, and every "
            "single removal\nwas tested and stopped reproducing. Neither author "
            "would find this alone, which is\nwhy it is worth reporting to both."
        )
    elif len(result.culprits) > 1:
        print(
            "\nMore than one mod is left, but minimality is unconfirmed — this "
            "is not yet an\ninteraction claim. See the note below."
        )
    if result.note:
        print(f"\nNote: {result.note}")

    if args.output:
        Path(args.output).write_text(json.dumps({
            "scenario": scenario_id,
            "metric": args.metric,
            "candidates": candidates,
            "culprits": list(result.culprits),
            "support": list(result.support),
            "converged": result.converged,
            "minimal": result.minimal,
            "revalidated": result.revalidated,
            "is_interaction": result.is_interaction,
            "probe_count": result.probe_count,
            "inconclusive_probes": result.inconclusive_probes,
            "invalid_probes": result.invalid_probes,
            "total_runs": oracle.total_runs,
            "successful_runs": oracle.successful_runs,
            "failed_runs": oracle.failed_runs,
            "note": result.note,
            "probes": oracle.audit(),
            "search": [
                {
                    "subset": list(p.subset),
                    "tested": list(p.tested),
                    "support": list(p.support),
                    "outcome": p.outcome.value,
                    "detail": p.detail,
                }
                for p in result.probes
            ],
        }, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")

    return 0 if result.converged else 1


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
    p.add_argument("--fdr", type=float, default=DEFAULT_FDR,
                   help="false-discovery rate for Benjamini-Hochberg control "
                        "across each scenario/metric family (default 0.05)")
    p.add_argument("--suite",
                   help="suite manifest to take interaction groups from, when "
                        "the results document does not carry them")
    p.add_argument("--reference-scenario", default=REFERENCE_SCENARIO,
                   help="scenario used as the hardware-normalisation "
                        "denominator (METHODOLOGY section 8)")
    p.add_argument("--export-dir",
                   help="write the full bundle: HTML report with charts, "
                        "Markdown, JSON, and data tables")
    p.add_argument("--table-format", action="append", default=None,
                   choices=["csv", "tsv", "md", "html"],
                   help="table formats for --export-dir (repeatable, default csv)")
    p.set_defaults(func=cmd_analyse)

    p = sub.add_parser("world",
                       help="fingerprint saved worlds and check they agree")
    p.add_argument("world", nargs="+",
                   help="world save directories (the ones holding level.dat)")
    p.add_argument("--dimension", default="region",
                   help="region directory: 'region' (overworld), DIM-1, DIM1")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_world)

    p = sub.add_parser("inspect", help="static health check of a mod set")
    p.add_argument("paths", nargs="+", help="jar files or directories of jars")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-mixins", action="store_true",
                   help="skip bytecode scanning (faster, loses contention data)")
    p.add_argument("--overlap-threshold", type=int, default=2,
                   help="report classes touched by at least this many mods")
    p.add_argument("--top", type=int, default=15,
                   help="how many contended classes to list")
    p.add_argument("--loader", choices=[loader.value for loader in Loader],
                   help="target loader; selects the version-range syntax "
                        "declared constraints are read in")
    p.add_argument("--minecraft-version",
                   help="target Minecraft version, so 'depends: minecraft "
                        ">=1.21' can be checked rather than merely recorded")
    p.add_argument("--loader-version",
                   help="target loader version, for the same reason")
    p.add_argument("--provisioned", action="append",
                   help="mod id the provisioning layer guarantees at a "
                        "compatible version (repeatable). Nothing is assumed "
                        "ambient without this — Fabric API included")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("targets", help="which scenarios run on which platforms")
    p.add_argument("--scenario-root")
    p.add_argument("--target", action="append",
                   help="platform:version, e.g. fabric:1.21.1 (repeatable)")
    p.add_argument("--with-mod", action="append",
                   help="assume this mod is present, e.g. carpet (repeatable)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("doctor", help="check whether this machine can measure")
    p.add_argument("--side", choices=["client", "server", "both"], default="both",
                   help="server-side runs do not need a GPU")
    p.add_argument("--heap-mb", type=int, default=4096)
    p.add_argument("--no-account-check", action="store_true",
                   help="skip the Minecraft account check")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="execute a suite headlessly")
    p.add_argument("suite")
    p.add_argument("--scenario-root")
    p.add_argument("--output", "-o", default="results.json")
    p.add_argument("--work-dir", default="work")
    p.add_argument("--headlessmc", help="path to the HeadlessMC jar or binary")
    p.add_argument("--local-root", help="root for resolving local: mod paths")
    p.add_argument("--agent-jar",
                   help="attach the mcbench JVM agent for frame timing; works "
                        "on any version and any loader, including ones with no "
                        "adapter (probe/adapters/probe-agent)")
    p.add_argument("--probe-jar",
                   help="the mcbench probe for this platform. Installed into "
                        "every instance including the baseline; without it "
                        "nothing in the game reads the scenario and no run "
                        "produces a measurement stream")
    p.add_argument("--fabric-api-jar",
                   help="Fabric API, which the Fabric probe hard-depends on. "
                        "Resolved from Modrinth when not given")
    p.add_argument("--timeout", type=float, help="per-run timeout in seconds")
    p.add_argument("--stop-on-failure", action="store_true")
    p.add_argument("--no-account-check", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="run despite preflight blockers; results are flagged "
                        "and must not be published")
    p.add_argument("--json-events", action="store_true",
                   help="emit newline-delimited JSON progress, for CI")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("bisect", help="isolate which mod causes a regression")
    p.add_argument("suite")
    p.add_argument("--scenario-root")
    p.add_argument("--scenario", help="which scenario to bisect on")
    p.add_argument("--mod", action="append",
                   help="candidate mod id (repeatable; default: all in the suite)")
    p.add_argument("--metric", default="frametime_mean_ms")
    p.add_argument("--runs-per-probe", type=int, default=5)
    p.add_argument("--rope", type=float, default=0.02)
    p.add_argument("--max-probes", type=int, default=200)
    p.add_argument("--reuse-baseline", action="store_true",
                   help="measure the baseline once instead of interleaving it "
                        "(cheaper, exploratory only)")
    p.add_argument("--work-dir", default="work")
    p.add_argument("--headlessmc")
    p.add_argument("--local-root")
    p.add_argument("--agent-jar")
    p.add_argument("--probe-jar")
    p.add_argument("--fabric-api-jar")
    p.add_argument("--timeout", type=float)
    p.add_argument("--no-account-check", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--output", "-o")
    p.set_defaults(func=cmd_bisect)

    p = sub.add_parser("metrics", help="list the metric registry")
    p.set_defaults(func=cmd_metrics)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ScenarioError, ConfigError, ResultsError) as exc:
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
