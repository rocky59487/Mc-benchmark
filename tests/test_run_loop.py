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
from dataclasses import replace
from pathlib import Path

import pytest
from executable import executable_python

from mcbench.config import parse_suite
from mcbench.metrics import RunFlag
from mcbench.runner import Harness
from mcbench.runner.harness import (
    HarnessError,
    ResolvedVariant,
    _java_release,
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
        # MCBENCH_STANDIN_REPORTS=key=value,key=value overrides what the game
        # says it is: a launcher that satisfied the request with something else.
        for pair in os.environ.get("MCBENCH_STANDIN_REPORTS", "").split(","):
            key, sep, value = pair.partition("=")
            if sep:
                event["metadata"][key] = value
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
        # The minimum a plan may declare. Only runs[0] is executed; this was
        # 'replicates: 1' until parse_suite started refusing keys it does not
        # read, at which point the key it should have been hit the floor
        # plan_runs enforces.
        "runs_per_cell": 5,
        **suite_overrides,
    })
    harness = Harness(
        suite, scenarios, work_dir=tmp_path / "work",
        headlessmc=stand_in(tmp_path / "stand-in", FIXTURES / fixture),
        probe_jar=probe_jar(tmp_path / "mcbench-probe.jar"),
        # These stand in for an operator who has accepted. The refusal itself
        # is tested in TestAcceptingTheEula, which builds without it.
        accept_eula=True,
    )
    for variant in suite.variants:
        harness._resolved[variant.name] = ResolvedVariant(variant=variant)
    return harness, harness.build_plan().runs[0], scenarios[scenario_id]


class TestLaunchingAServerDirectly:
    """HeadlessMC has no server command, and the harness said so and stopped.
    That left eight of eleven scenarios and eleven of twenty-eight metrics with
    no way to run at all, on a tool whose first claim is client *and* server.

    A dedicated server needs a jar and a working directory. No account, no
    asset download, nothing a launcher is for.
    """

    def server(self, tmp_path, **extra):
        harness, planned, scenario = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl",
            **extra.pop("suite_overrides", {}),
        )
        for key, value in extra.items():
            setattr(harness, key, value)
        return harness, planned, scenario

    def a_jar(self, tmp_path) -> Path:
        path = tmp_path / "fabric-server-launch.jar"
        path.write_bytes(b"PK\x03\x04not-really-a-server")
        return path

    def test_the_command_is_java_on_the_jar(self, tmp_path):
        jar = self.a_jar(tmp_path)
        harness, _, scenario = self.server(tmp_path, server_jar=jar)

        command = harness._launch_command(tmp_path / "instance", scenario)

        assert command[0] == "java"
        assert command[-2:] == [str(jar.resolve()), "nogui"]
        # The heap the suite asked for has to reach the server, or every
        # variant runs at whatever the JVM defaults to.
        assert any(arg.startswith("-Xmx") for arg in command)

    def test_it_runs_from_the_instance(self, tmp_path):
        # The server writes its world, its logs and its eula.txt relative to
        # the working directory. Anywhere else and the run measures a world
        # nothing prepared.
        harness, _, scenario = self.server(tmp_path, server_jar=self.a_jar(tmp_path))
        instance = tmp_path / "instance"

        assert harness._launch_cwd(instance, scenario) == instance

    def test_a_client_still_goes_through_the_launcher(self, tmp_path):
        harness, _, _ = self.server(tmp_path, server_jar=self.a_jar(tmp_path))
        client = harness.scenarios["reference-hardware-baseline"]

        command = harness._launch_command(tmp_path / "instance", client)

        assert "nogui" not in command
        assert str(harness.headlessmc) in " ".join(command)

    def test_a_jar_that_is_not_there_says_so_before_launching(self, tmp_path):
        harness, _, scenario = self.server(
            tmp_path, server_jar=tmp_path / "absent.jar"
        )
        with pytest.raises(HarnessError, match="does not name a file"):
            harness._launch_command(tmp_path / "instance", scenario)

    def test_without_a_jar_the_refusal_names_the_way_out(self, tmp_path):
        harness, _, scenario = self.server(tmp_path, server_jar=None, headlessmc=None)
        with pytest.raises(HarnessError) as raised:
            harness._launch_command(tmp_path / "instance", scenario)
        assert "--server-jar" in str(raised.value)

    def test_a_client_refusal_does_not_mention_it(self, tmp_path):
        # Suggesting --server-jar to somebody running a client scenario sends
        # them after a jar that would not be used.
        harness, _, _ = self.server(tmp_path, headlessmc=None)
        client = harness.scenarios["reference-hardware-baseline"]

        with pytest.raises(HarnessError) as raised:
            harness._launch_command(tmp_path / "instance", client)
        assert "--server-jar" not in str(raised.value)


class TestAcceptingTheEula:
    """eula.txt is what Mojang reads to decide the operator agreed. mcbench
    wrote it unconditionally, with a comment asserting that running the tool
    was the acceptance, so a server run recorded an agreement nobody had
    made."""

    def unaccepted(self, tmp_path):
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness.accept_eula = False
        return harness, planned

    def test_a_server_run_refuses_without_it(self, tmp_path):
        harness, planned = self.unaccepted(tmp_path)
        outcome = harness.execute_run(planned)

        assert not outcome.succeeded
        assert "EULA" in outcome.error
        # The refusal has to say where to read it and how to say yes, or it is
        # just an obstacle.
        assert "aka.ms/MinecraftEULA" in outcome.error
        assert "--accept-eula" in outcome.error

    def test_nothing_is_written_when_it_is_refused(self, tmp_path):
        harness, planned = self.unaccepted(tmp_path)
        harness.execute_run(planned)

        written = list((tmp_path / "work" / "instances").rglob("eula.txt"))
        assert written == []

    def test_accepting_writes_it_and_says_who(self, tmp_path):
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness.execute_run(planned)

        written = list((tmp_path / "work" / "instances").rglob("eula.txt"))
        assert len(written) == 1
        text = written[0].read_text(encoding="utf-8")
        assert "eula=true" in text
        assert "--accept-eula" in text

    def test_a_client_run_needs_nothing(self, tmp_path):
        # The client is launched through a launcher that handles its own
        # licensing, and no eula.txt is involved.
        harness, planned, _ = build(
            tmp_path, "reference-hardware-baseline", "probe-client-reference.jsonl"
        )
        harness.accept_eula = False
        outcome = harness.execute_run(planned)

        # Not "the run succeeded": a client run against a stand-in has its own
        # reasons not to, and asserting the whole path here would make this test
        # fail for things it is not about. The claim is only that the licence
        # was never the obstacle and nothing was signed.
        assert "EULA" not in outcome.error
        assert list((tmp_path / "work" / "instances").rglob("eula.txt")) == []

    def test_the_manifest_can_carry_it(self):
        suite = parse_suite({
            "name": "s", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["tick-stability-saturated"],
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base", "accept_eula": True,
        })
        assert suite.accept_eula is True

    def test_the_manifest_defaults_to_not_accepted(self):
        suite = parse_suite({
            "name": "s", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["tick-stability-saturated"],
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
        })
        assert suite.accept_eula is False

    def test_provenance_records_the_answer(self, tmp_path):
        harness, _, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        assert harness.provenance()["eula_accepted"] is True
        harness.accept_eula = False
        assert harness.provenance()["eula_accepted"] is False


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


class TestReadingAJavaBanner:
    """``java -version`` prints a banner; the game reports a bare release. The
    comparison needs the part they share, and a parse that quietly returned
    nothing would disable the check rather than fail it."""

    def test_the_release_comes_out_of_the_banner(self):
        assert _java_release('openjdk version "21.0.11" 2026-04-15 LTS') == "21.0.11"
        assert _java_release('java version "1.8.0_402"') == "1.8.0_402"
        assert _java_release('openjdk version "17.0.9" 2023-10-17') == "17.0.9"
        assert _java_release('openjdk version "24-ea" 2025-03-18') == "24-ea"

    def test_nothing_recognisable_yields_nothing(self):
        # Empty disables the comparison for this field, which is the right
        # answer: an unparsed banner is not evidence of disagreement.
        assert _java_release("unknown") == ""
        assert _java_release("") == ""
        assert _java_release('some tool "not-a-version-at-all"') == ""


class TestWhatTheGameSaysItWas:
    """The probe reports its own identity; the harness records what it asked
    for. Anything that satisfied the request differently shows up here."""

    def run_reporting(self, tmp_path, monkeypatch, **reported):
        if reported:
            monkeypatch.setenv(
                "MCBENCH_STANDIN_REPORTS",
                ",".join(f"{k}={v}" for k, v in reported.items()),
            )
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        # Pinned rather than asked of the machine: whether a JVM is installed
        # is not what any of these are testing. The value is the one the
        # fixture's own hello carries, so an un-overridden run agrees.
        harness._java_release = "21.0.10"
        return harness.execute_run(planned)

    def test_a_run_that_matches_says_nothing(self, tmp_path, monkeypatch):
        outcome = self.run_reporting(
            tmp_path, monkeypatch,
            platform="fabric", minecraft_version="1.21.1",
            loader_version="0.16.5", java="21.0.10",
        )
        assert outcome.configuration_mismatches == []
        assert RunFlag.CONFIGURATION_MISMATCH not in outcome.metrics.flags
        assert outcome.succeeded

    def test_fields_the_probe_omits_are_not_disagreements(
        self, tmp_path, monkeypatch
    ):
        # The stand-in reports only scenario and scenario_hash, as an older
        # probe or a platform without the concept would.
        outcome = self.run_reporting(tmp_path, monkeypatch)
        assert outcome.configuration_mismatches == []
        assert outcome.succeeded

    def test_another_minecraft_is_not_pooled(self, tmp_path, monkeypatch):
        outcome = self.run_reporting(tmp_path, monkeypatch, minecraft_version="1.20.1")

        assert outcome.configuration_mismatches == [
            "minecraft_version: recorded 1.21.1, game reported 1.20.1"
        ]
        assert RunFlag.CONFIGURATION_MISMATCH in outcome.metrics.flags
        # The numbers are real and look entirely ordinary, which is the whole
        # reason this has to be refused rather than noted.
        assert outcome.metrics.values["mspt_mean"] > 0
        assert not outcome.succeeded
        assert outcome.status == "inadmissible"

    def test_another_loader_build_is_not_pooled(self, tmp_path, monkeypatch):
        outcome = self.run_reporting(tmp_path, monkeypatch, loader_version="0.15.0")
        assert RunFlag.CONFIGURATION_MISMATCH in outcome.metrics.flags
        assert not outcome.succeeded

    def test_another_jvm_is_recorded_but_still_pooled(self, tmp_path, monkeypatch):
        # A launcher free to pick its own JVM picked one. Every variant ran
        # under it, so nothing about the comparison changed; only the string
        # the provenance block would have published is wrong, and both values
        # are now in the record.
        outcome = self.run_reporting(tmp_path, monkeypatch, java="17.0.9")

        assert outcome.configuration_mismatches == [
            "java: recorded 21.0.10, game reported 17.0.9"
        ]
        assert RunFlag.CONFIGURATION_MISMATCH not in outcome.metrics.flags
        assert outcome.succeeded

    def test_the_record_carries_what_the_game_said(self, tmp_path, monkeypatch):
        outcome = self.run_reporting(
            tmp_path, monkeypatch, minecraft_version="1.20.1", java="17.0.9"
        )
        record = outcome.to_record()

        assert record["reported"]["minecraft_version"] == "1.20.1"
        assert record["reported"]["java"] == "17.0.9"
        assert len(record["configuration_mismatches"]) == 2
        assert record["status"] == "inadmissible"

    def test_a_matching_run_records_the_report_without_a_mismatch_key(
        self, tmp_path, monkeypatch
    ):
        record = self.run_reporting(tmp_path, monkeypatch).to_record()
        assert record["reported"]["scenario"] == "entity-mobcap-saturation"
        assert "configuration_mismatches" not in record

    def client_run_reporting(self, tmp_path, monkeypatch, **reported):
        if reported:
            monkeypatch.setenv(
                "MCBENCH_STANDIN_REPORTS",
                ",".join(f"{k}={v}" for k, v in reported.items()),
            )
        scenario_id = next(
            s.id for s in load_scenarios(REPO / "scenarios") if s.side is Side.CLIENT
        )
        harness, planned, _ = build(
            tmp_path, scenario_id, "probe-client-reference.jsonl"
        )
        harness._java_release = ""
        return harness.execute_run(planned)

    def test_the_window_the_client_rendered_at_is_checked(
        self, tmp_path, monkeypatch
    ):
        # Resolution decides what a frame time means, and the launcher is free
        # to ignore the size it was asked for. Nothing else in the run would
        # show that it had.
        outcome = self.client_run_reporting(
            tmp_path, monkeypatch, window="1280x720"
        )

        assert outcome.configuration_mismatches == [
            "window: recorded 1920x1080, game reported 1280x720"
        ]
        # Every variant renders through the same launcher, so this makes the
        # published description wrong rather than the comparison invalid. The
        # client fixture is too short to be admissible on its own account, so
        # the absence of this flag is the claim, not the run's admissibility.
        assert RunFlag.CONFIGURATION_MISMATCH not in outcome.metrics.flags

    def test_the_asked_for_window_says_nothing(self, tmp_path, monkeypatch):
        outcome = self.client_run_reporting(
            tmp_path, monkeypatch, window="1920x1080"
        )
        assert outcome.configuration_mismatches == []

    def test_a_suite_that_asked_for_another_size_is_held_to_that(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("MCBENCH_STANDIN_REPORTS", "window=1920x1080")
        scenario_id = next(
            s.id for s in load_scenarios(REPO / "scenarios") if s.side is Side.CLIENT
        )
        harness, planned, _ = build(
            tmp_path, scenario_id, "probe-client-reference.jsonl",
            game_settings={"resolution": "1280x720"},
        )
        harness._java_release = ""
        outcome = harness.execute_run(planned)

        assert outcome.configuration_mismatches == [
            "window: recorded 1280x720, game reported 1920x1080"
        ]

    def test_a_server_run_is_not_asked_about_a_window(self, tmp_path, monkeypatch):
        # It has none, and comparing against the client default would flag
        # every server run in every suite.
        outcome = self.run_reporting(tmp_path, monkeypatch, window="1280x720")
        assert outcome.configuration_mismatches == []


class TestTheServerGeneratorReachesTheServer:
    """A server was told the seed and nothing about the generator.

    world.py maps a scenario's generator name for a client and says why: a flat
    scenario opened on default terrain is a different benchmark that still
    produces numbers. Six of the eight shipped server scenarios ask for flat
    and were getting the ordinary overworld, with their structures placed at
    y=5 — open air on one, solid stone on the other.
    """

    def properties(self, tmp_path, scenario_id):
        harness, planned, _ = build(
            tmp_path, scenario_id, "probe-server-reference.jsonl"
        )
        harness.execute_run(planned)
        text = (harness._instance_dir(planned) / "server.properties").read_text(
            encoding="utf-8"
        )
        return dict(
            line.split("=", 1) for line in text.splitlines() if "=" in line
        )

    def test_a_flat_scenario_says_flat(self, tmp_path):
        found = self.properties(tmp_path, "entity-mobcap-saturation")
        assert found["level-type"] == "minecraft:flat"
        assert "grass_block" in found["generator-settings"]

    def test_a_default_scenario_says_normal_and_needs_no_settings(self, tmp_path):
        found = self.properties(tmp_path, "chunkio-load-throughput")
        assert found["level-type"] == "minecraft:normal"
        assert "generator-settings" not in found

    def test_the_two_paths_describe_the_same_flat_world(self):
        """The client writes layers into level.dat; the server gets JSON.

        Two spellings of one world, and nothing would notice them drifting
        apart except a fingerprint mismatch between a client and a server run
        of scenarios that are supposed to be comparable.
        """
        from mcbench.world import _OVERWORLD_GENERATORS

        level_type, server = Harness.SERVER_GENERATORS["flat"]
        assert level_type == "minecraft:flat"

        client = _OVERWORLD_GENERATORS["flat"]["settings"]
        assert server["biome"] == client["biome"]
        assert [
            (layer["block"], layer["height"]) for layer in server["layers"]
        ] == [
            (layer["block"], int(layer["height"])) for layer in client["layers"]
        ]

    def test_a_scenario_that_names_its_layers_gets_them(self, tmp_path):
        # Six scenarios declare world.generator_settings and nothing read it,
        # so each was given the generator's built-in layers. This one asks for
        # three of stone where the default has two of dirt.
        found = self.properties(tmp_path, "entity-mobcap-saturation")
        settings = json.loads(found["generator-settings"])
        assert [
            (layer["block"], layer["height"]) for layer in settings["layers"]
        ] == [
            ("minecraft:bedrock", 1),
            ("minecraft:stone", 3),
            ("minecraft:grass_block", 1),
        ]

    def test_an_unmapped_generator_is_refused(self, tmp_path):
        # Defaulting would generate ordinary terrain for a scenario that named
        # something else, which is the failure this exists to prevent.
        harness, planned, scenario = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness.scenarios[scenario.id] = replace(
            scenario, world={**scenario.world, "generator": "swiss_cheese"}
        )
        outcome = harness.execute_run(planned)
        assert outcome.metrics is None
        assert "swiss_cheese" in outcome.error


class TestTheOptionsTheClientGot:
    def test_the_scenario_distances_reach_options_txt(self, tmp_path):
        """Both distances, spelled the way options.txt spells them.

        The scenario writes snake_case and options.txt does not, so each needs
        translating. Render distance was translated; simulation distance was
        excluded from the passthrough beside it and never given a line of its
        own, so a client scenario asking for one got the game's default — for
        the setting that decides how much of the world ticks around the player.
        """
        scenario_id = next(
            s.id for s in load_scenarios(REPO / "scenarios") if s.side is Side.CLIENT
        )
        harness, planned, scenario = build(
            tmp_path, scenario_id, "probe-client-reference.jsonl"
        )
        harness.execute_run(planned)

        options = dict(
            line.split(":", 1)
            for line in (harness._instance_dir(planned) / "options.txt")
            .read_text(encoding="utf-8").splitlines()
            if ":" in line
        )
        declared = {
            step["op"]: step for step in scenario.setup
            if step.get("op", "").startswith("set_")
        }
        # Both, named rather than looked for: a test that checks whichever
        # settings the scenario happens to declare passes without checking
        # anything the day one of them is dropped, which is the failure being
        # guarded against.
        assert "set_render_distance" in declared
        assert "set_simulation_distance" in declared
        assert options["renderDistance"] == str(
            declared["set_render_distance"]["chunks"]
        )
        assert options["simulationDistance"] == str(
            declared["set_simulation_distance"]["chunks"]
        )
        # And nothing snake_case leaked through, which the game would ignore.
        assert not [key for key in options if "_" in key], sorted(options)


class TestWhoMadeTheWorld:
    """A refused fingerprint reads as a mod altering worldgen. Often it is not.

    The run that generates a scenario's world measures the least settled
    terrain of any run of that scenario, because chunks at the edge of
    generation keep changing while their neighbours are made. It can therefore
    disagree with every run that restores its cache, and the majority rule
    refuses it. On this repository's own flyby that is 5 chunks of 1089, all in
    the outermost three rings, and the run it costs is whichever variant the
    shuffle happened to put first.
    """

    def test_the_first_run_of_a_scenario_says_it_generated(self, tmp_path):
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        record = harness.execute_run(planned).to_record()
        assert record["world_source"] == "generated"

    def test_a_later_run_says_it_was_handed_one(self, tmp_path):
        harness, plan_first, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness.execute_run(plan_first)
        later = harness.build_plan().runs[1]
        record = harness.execute_run(later).to_record()

        assert record["world_source"] == "restored"
        assert later.cell != plan_first.cell or later.replicate != plan_first.replicate


class TestTheShapeOfTheDistribution:
    def test_a_client_run_records_its_frametime_sketch(self, tmp_path):
        """Without it the CDF chart the README promises has no data source.

        A results document carried percentiles and nothing else, so the chart
        was implemented, wired into the renderer, and never once drawn.
        """
        scenario_id = next(
            s.id for s in load_scenarios(REPO / "scenarios") if s.side is Side.CLIENT
        )
        harness, planned, _ = build(
            tmp_path, scenario_id, "probe-client-reference.jsonl"
        )
        record = harness.execute_run(planned).to_record()

        sketch = record["frametime_sketch_ms"]
        assert sketch == sorted(sketch)
        assert sketch[0] > 0
        # It has to agree with the summary it travels beside, or the chart and
        # the table would describe different runs.
        assert sketch[-1] == pytest.approx(record["values"]["frametime_p99_ms"], rel=0.5)

    def test_a_server_run_records_none(self, tmp_path):
        # No frames to shape. An empty list would be a claim that the
        # distribution was measured and found empty.
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        assert "frametime_sketch_ms" not in harness.execute_run(planned).to_record()


class TestSomethingElseOnTheMachine:
    """Preflight checks for competing Minecraft processes once, and a suite
    runs for hours. Anything that starts after it passed is invisible to it."""

    def run_seeing(self, tmp_path, monkeypatch, *samples):
        # One return value per call: before the launch, then after it.
        remaining = list(samples)
        monkeypatch.setattr(
            "mcbench.runner.harness.competing_minecraft",
            lambda: remaining.pop(0) if remaining else [],
        )
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness._java_release = "21.0.10"
        return harness.execute_run(planned)

    def test_a_quiet_machine_is_not_flagged(self, tmp_path, monkeypatch):
        outcome = self.run_seeing(tmp_path, monkeypatch, [], [])
        assert RunFlag.ENVIRONMENT_NOISY not in outcome.metrics.flags
        assert outcome.succeeded

    def test_something_that_started_mid_suite_is_flagged(
        self, tmp_path, monkeypatch
    ):
        # Absent when the run began, running when it ended: exactly the case
        # preflight cannot see.
        outcome = self.run_seeing(
            tmp_path, monkeypatch, [], ["4242 java -cp net.fabricmc.loader"]
        )
        assert RunFlag.ENVIRONMENT_NOISY in outcome.metrics.flags
        # Recorded, not refused. Whether a competitor mattered depends on what
        # it was doing, which this cannot know, so the reader decides.
        assert outcome.succeeded
        assert "environment_noisy" in outcome.to_record()["flags"]

    def test_one_seen_only_before_the_launch_still_counts(
        self, tmp_path, monkeypatch
    ):
        outcome = self.run_seeing(
            tmp_path, monkeypatch, ["99 java net.minecraft.client.main.Main"], []
        )
        assert RunFlag.ENVIRONMENT_NOISY in outcome.metrics.flags

    def test_the_same_process_either_side_is_counted_once(
        self, tmp_path, monkeypatch
    ):
        seen: list[dict] = []
        line = "77 java -cp neoforged"
        remaining = [[line], [line]]
        monkeypatch.setattr(
            "mcbench.runner.harness.competing_minecraft",
            lambda: remaining.pop(0) if remaining else [],
        )
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness._java_release = "21.0.10"
        harness.on_event = lambda event, payload: (
            seen.append(payload) if event == "run.noisy" else None
        )
        harness.execute_run(planned)
        assert [p["processes"] for p in seen] == [1]

    def test_an_unreadable_process_table_is_not_a_finding(
        self, tmp_path, monkeypatch
    ):
        # competing_minecraft returns None when it could not look. Preflight
        # reports that once, where it will be read; a flag per run would say
        # the same thing sixty times and come to mean "we did not look".
        monkeypatch.setattr(
            "mcbench.runner.harness.competing_minecraft", lambda: None
        )
        harness, planned, _ = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness._java_release = "21.0.10"
        outcome = harness.execute_run(planned)
        assert RunFlag.ENVIRONMENT_NOISY not in outcome.metrics.flags


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



class TestAFailedAttemptClaimsNothing:
    def test_a_run_that_never_started_has_no_world_source(self, tmp_path):
        """world_source answers "did this run make its world or get one".

        A run that died preparing its instance has no answer. Emitting the
        default reads as "generated", which is the value that makes a refused
        fingerprint look like the run's own doing.
        """
        harness, planned, scenario = build(
            tmp_path, "entity-mobcap-saturation", "probe-server-reference.jsonl"
        )
        harness.scenarios[scenario.id] = replace(
            scenario, world={**scenario.world, "generator": "swiss_cheese"}
        )
        record = harness.execute_run(planned).to_record()

        assert record["status"] == "failed"
        assert "world_source" not in record
        assert "swiss_cheese" in record["error"]
