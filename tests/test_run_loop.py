"""One run, executed end to end against a stand-in game.

Every piece of `execute_run` was covered in isolation and the path through it
had never run: the instance it prepares, the environment it exports, the file
it waits for, the stream it parses, and the outcome it builds. A stand-in that
plays the game's side of that contract — read the configuration, write a
stream, save a world — is the only way to find out whether the two halves agree
about where anything is.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from executable import executable_python

from mcbench.config import parse_suite
from mcbench.metrics import RunFlag
from mcbench.runner import Harness
from mcbench.runner.harness import (
    ResolvedVariant,
    flag_world_mismatches,
    outcomes_to_cells,
    run_counts,
)
from mcbench.scenario import Side, load_scenarios

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"

STAND_IN = '''"""Stands in for HeadlessMC and the game it launches."""
import json
import os
import sys
from pathlib import Path

HELP = """usage: stand-in launch <version> [options]
  --gamedir <dir>              game directory
  --jvm <args>                 jvm arguments
  --loader <name>              mod loader
  --loader-version <version>   loader build
  --quickPlaySingleplayer <w>  enter a world directly
  --server                     launch the dedicated server
"""

if "--help" in sys.argv or "help" in sys.argv[1:2]:
    print(HELP)
    raise SystemExit(0)

sys.path.insert(0, {src!r})
sys.path.insert(0, {tests!r})
from mcbench.world import create_world  # noqa: E402
from test_world import _chunk, _write_region  # noqa: E402


def fail(message):
    print("stand-in: " + message, file=sys.stderr)
    raise SystemExit(3)


config = Path(os.environ.get("MCBENCH_PROBE_CONFIG", ""))
output = Path(os.environ.get("MCBENCH_PROBE_OUTPUT", ""))
if not config.is_file():
    fail("MCBENCH_PROBE_CONFIG names no file: " + str(config))
if not str(output):
    fail("MCBENCH_PROBE_OUTPUT was not set")

properties = {{}}
for line in config.read_text(encoding="utf-8").splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        properties[key.strip()] = value.strip()

# What a real probe reads before it can do anything: which scenario, where to
# write, and the command lists compiled for it.
for required in ("scenario.id", "scenario.content_hash", "output", "measurement.duration"):
    if required not in properties:
        fail("probe.properties has no " + required)
for name in ("setup.txt", "workload.txt"):
    if not (config.parent / name).is_file():
        fail("the plan is missing " + name)

argv = sys.argv[1:]
gamedir = Path(argv[argv.index("--gamedir") + 1]) if "--gamedir" in argv else None
if gamedir is None:
    fail("no --gamedir")
if "--server" in argv:
    # The server generates its world on first start; the harness only names it
    # and states the seed.
    server = {{}}
    for line in (gamedir / "server.properties").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            server[key] = value
    world = create_world(
        gamedir,
        name=server.get("level-name", "world"),
        seed=int(server.get("level-seed", "0")),
    )
    if os.environ.get("MCBENCH_STANDIN_EMPTY_WORLD") != "1":
        # The terrain a server generates on first start, which is what the
        # harness fingerprints. A variant named in MCBENCH_STANDIN_ROGUE
        # generates different terrain — a mod that alters worldgen.
        rogue = os.environ.get("MCBENCH_STANDIN_ROGUE", "")
        block = "minecraft:andesite" if rogue and rogue in gamedir.name else "minecraft:stone"
        (world / "region").mkdir(parents=True, exist_ok=True)
        _write_region(world / "region" / "r.0.0.mca", {{(0, 0): _chunk(block)}})

stream = Path({stream!r}).read_text(encoding="utf-8").splitlines()
lines = []
for raw in stream:
    event = json.loads(raw)
    if event.get("type") == "fingerprint" and os.environ.get("MCBENCH_STANDIN_DROP_WORLD") == "1":
        # A probe that could not fingerprint the live world; the harness falls
        # back to the save on disk.
        continue
    if event.get("type") == "hello":
        # The metadata a real adapter reports about the run it was configured for.
        event["metadata"] = dict(event.get("metadata", {{}}))
        event["metadata"]["scenario"] = properties["scenario.id"]
        event["metadata"]["scenario_hash"] = properties["scenario.content_hash"]
    lines.append(json.dumps(event))

output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
print("stand-in: wrote " + str(len(lines)) + " events")
'''


def stand_in(path: Path, stream: Path) -> Path:
    return executable_python(
        path,
        STAND_IN.format(
            src=str(REPO / "src"), tests=str(REPO / "tests"), stream=str(stream)
        ),
    )


def probe_jar(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"mcbench-probe"}')
    return path


def build(tmp_path, scenario_id: str, fixture: str, **suite_overrides):
    scenarios = {s.id: s for s in load_scenarios(REPO / "scenarios")}
    suite = parse_suite({
        "name": "e2e", "minecraft_version": "1.21.1", "loader": "fabric",
        "loader_version": "0.16.5",
        "scenarios": [scenario_id],
        "variants": [{"name": "base", "mods": []}],
        "baseline": "base",
        "replicates": 1,
        **suite_overrides,
    })
    harness = Harness(
        suite, scenarios, work_dir=tmp_path / "work",
        headlessmc=stand_in(tmp_path / "stand-in", FIXTURES / fixture),
        probe_jar=probe_jar(tmp_path / "mcbench-probe.jar"),
    )
    for variant in suite.variants:
        harness._resolved[variant.name] = ResolvedVariant(variant=variant)
    return harness, harness.build_plan().runs[0], scenarios[scenario_id]


class TestOneServerRun:
    def test_the_run_produces_metrics(self, tmp_path):
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        outcome = harness.execute_run(planned)

        assert outcome.error == "", outcome.log_path.read_text(encoding="utf-8")
        assert outcome.exit_code == 0
        assert outcome.succeeded
        assert outcome.status == "completed"
        assert outcome.metrics is not None
        assert outcome.metrics.sample_count > 0
        assert outcome.metrics.values["mspt_mean"] > 0

    def test_the_stand_in_read_the_configuration_the_harness_wrote(self, tmp_path):
        # It exits 3 with a message if any of it is missing, so an ordinary
        # outcome is the assertion. The failure this guards against is a
        # renamed key or a moved file, which no unit test on either side sees.
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        outcome = harness.execute_run(planned)
        assert outcome.exit_code == 0, outcome.log_path.read_text(encoding="utf-8")

    def test_the_probes_own_fingerprint_wins(self, tmp_path):
        # Taken over the live world while the measured region was loaded, which
        # the harness cannot see from the save alone.
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        outcome = harness.execute_run(planned)
        assert outcome.world_fingerprint == "selftest-synthetic-world"

    def test_the_harness_fingerprints_the_save_when_the_probe_cannot(
        self, tmp_path, monkeypatch
    ):
        # Without this the pooling rule is a promise the code does not keep on
        # any platform whose probe has no world API.
        monkeypatch.setenv("MCBENCH_STANDIN_DROP_WORLD", "1")
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        outcome = harness.execute_run(planned)
        assert len(outcome.world_fingerprint) == 64

    def test_a_world_with_no_terrain_is_not_a_fingerprint(
        self, tmp_path, monkeypatch
    ):
        # Hashing nothing produces the same constant every time, so two runs
        # that generated no terrain would agree and be pooled on a comparison
        # that read nothing.
        monkeypatch.setenv("MCBENCH_STANDIN_DROP_WORLD", "1")
        monkeypatch.setenv("MCBENCH_STANDIN_EMPTY_WORLD", "1")
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        outcome = harness.execute_run(planned)
        assert outcome.world_fingerprint == ""

    def test_the_instance_holds_the_probe_and_the_plan(self, tmp_path):
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness.execute_run(planned)
        instance = harness._instance_dir(planned)
        assert (instance / "mods" / "mcbench-probe.jar").is_file()
        assert (instance / "mcbench" / "probe.jsonl").is_file()
        recorded = json.loads((instance / "mcbench" / "scenario.json").read_text())
        assert recorded["id"] == "entity-mobcap-saturation"

    def test_the_outcome_serialises_with_its_run_counted(self, tmp_path):
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        outcome = harness.execute_run(planned)
        record = outcome.to_record()
        assert record["status"] == "completed"
        assert record["values"]["mspt_mean"] > 0
        counts = run_counts([outcome])
        assert counts["entity-mobcap-saturation/base"]["attempted"] == 1
        assert counts["entity-mobcap-saturation/base"]["failed"] == 0


class TestOneClientRun:
    def test_the_authored_world_survives_the_round_trip(self, tmp_path):
        scenario_id = next(
            s.id for s in load_scenarios(REPO / "scenarios") if s.side is Side.CLIENT
        )
        harness, planned, scenario = build(
            tmp_path, scenario_id, "probe-client-reference.jsonl"
        )
        outcome = harness.execute_run(planned)

        assert outcome.error == "", outcome.log_path.read_text(encoding="utf-8")
        assert outcome.metrics is not None
        assert outcome.metrics.values["fps_avg"] > 0

        # The harness wrote level.dat before the launch and read it back after,
        # and agreed with itself about which world and which seed — the check
        # that makes a client-generated world detectable rather than measured.
        instance = harness._instance_dir(planned)
        world = instance / "saves" / harness._client_world_name(scenario)
        assert world.is_dir()
        assert harness._client_world_mismatch(instance, scenario, world) == ""
        # No fingerprint, because a stand-in generates no terrain to hash. A
        # real client leaves region files; an empty hash is refused either way,
        # so this run would never be pooled.
        assert outcome.world_fingerprint == ""


class TestARunThatFails:
    def test_a_game_that_writes_nothing_is_recorded_as_failed(self, tmp_path):
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        Path(harness.headlessmc).write_text(
            "#!/bin/sh\nif [ \"$1\" = \"--help\" ]; then\n"
            "  echo '  --gamedir <dir>'\n  echo '  --jvm <args>'\n  exit 0\nfi\n"
            "echo 'crashed before the probe loaded' >&2\nexit 1\n",
            encoding="utf-8",
        )
        outcome = harness.execute_run(planned)

        assert not outcome.succeeded
        assert outcome.metrics is None
        assert outcome.status == "failed"
        assert outcome.error
        # The attempt is still counted; dropping it would make a mod that
        # crashes half its runs look like one with fewer, cleaner runs.
        counts = run_counts([outcome])
        assert counts["entity-mobcap-saturation/base"]["attempted"] == 1
        assert counts["entity-mobcap-saturation/base"]["failed"] == 1


class TestAWholeSuite:
    """Several runs in the plan's order, then the document an analysis reads."""

    def suite(self, tmp_path, monkeypatch, *, fresh_world: bool = False):
        monkeypatch.setenv("MCBENCH_STANDIN_DROP_WORLD", "1")
        harness, _, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl",
            variants=[{"name": "base", "mods": []}, {"name": "candidate", "mods": []}],
            runs_per_cell=5,
        )
        harness.fresh_world = fresh_world
        return harness

    def test_a_clean_suite(self, tmp_path, monkeypatch):
        # Two variants over one scenario, ten runs. Asserted together because
        # executing the suite is the expensive part, not the checking.
        harness = self.suite(tmp_path, monkeypatch)
        outcomes = harness.run_suite()

        assert len(outcomes) == 10
        assert [o.planned.position for o in outcomes] == list(range(10))
        assert all(o.succeeded for o in outcomes)

        counts = run_counts(outcomes)
        assert counts["entity-mobcap-saturation/base"]["admissible"] == 5
        assert counts["entity-mobcap-saturation/candidate"]["admissible"] == 5

        cells = outcomes_to_cells(outcomes)
        assert sorted(cells) == [
            "entity-mobcap-saturation/base", "entity-mobcap-saturation/candidate",
        ]
        assert all(len(runs) == 5 for runs in cells.values())

        # Same terrain in every instance, so nothing is held out.
        assert flag_world_mismatches(outcomes) == {}

    def test_a_variant_that_alters_worldgen_is_not_pooled(
        self, tmp_path, monkeypatch
    ):
        # The pooling rule, enforced rather than promised: the candidate's
        # terrain differs, so its runs carry the mismatch flag and the numbers,
        # which look entirely ordinary, never enter a comparison.
        #
        # fresh_world, because that is the mode this question belongs to. With
        # the shared world every run measures the terrain the first one
        # generated, and a mod cannot show that it would have generated
        # something else.
        monkeypatch.setenv("MCBENCH_STANDIN_ROGUE", "candidate")
        harness = self.suite(tmp_path, monkeypatch, fresh_world=True)
        outcomes = harness.run_suite()

        mismatches = flag_world_mismatches(outcomes)
        assert mismatches["entity-mobcap-saturation"]

        # Five runs each, so neither world holds a majority and there is no
        # reference. Both sides are flagged rather than one being blamed by
        # whichever fingerprint sorted higher, which is what used to decide it.
        flagged = {
            o.planned.cell.variant for o in outcomes
            if o.metrics and RunFlag.WORLD_FINGERPRINT_MISMATCH in o.metrics.flags
        }
        assert flagged == {"base", "candidate"}
        assert not any(o.metrics.admissible for o in outcomes if o.metrics)

    def test_a_minority_world_is_flagged_against_the_majority(
        self, tmp_path, monkeypatch
    ):
        """With a real majority the odd runs out are the ones flagged."""
        monkeypatch.setenv("MCBENCH_STANDIN_DROP_WORLD", "1")
        monkeypatch.setenv("MCBENCH_STANDIN_ROGUE", "candidate")
        harness, _, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl",
            variants=[
                {"name": "base", "mods": []},
                {"name": "second", "mods": []},
                {"name": "candidate", "mods": []},
            ],
            runs_per_cell=5,
        )
        harness.fresh_world = True
        outcomes = harness.run_suite()
        flag_world_mismatches(outcomes)

        flagged = {
            o.planned.cell.variant for o in outcomes
            if o.metrics and RunFlag.WORLD_FINGERPRINT_MISMATCH in o.metrics.flags
        }
        assert flagged == {"candidate"}

