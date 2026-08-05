"""Launcher capability probing.

Every flag the harness puts on a launch command is an assumption about a tool it
does not ship. Probing turns a wrong assumption into a preflight message instead
of a launch that fails minutes in, once per planned run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from executable import printing_stand_in

from mcbench.config import parse_suite
from mcbench.runner import Harness, Severity
from mcbench.runner.launcher import probe_launcher
from mcbench.scenario import Side, load_scenarios

REPO = Path(__file__).resolve().parents[1]


def fake_launcher(path: Path, help_text: str) -> Path:
    return printing_stand_in(path, help_text)


FULL_HELP = """
usage: headlessmc launch <version> [options]
  --gamedir <dir>              game directory
  --jvm <args>                 jvm arguments
  --loader <name>              mod loader
  --loader-version <version>   loader build
  --quickPlaySingleplayer <w>  enter a world directly
  --server                     launch the dedicated server
"""

MINIMAL_HELP = """
usage: launcher launch <version>
  --gamedir <dir>   game directory
  --jvm <args>      jvm arguments
"""


def harness(tmp_path, launcher, *, scenario="entity-mobcap-saturation", **extra):
    scenarios = {s.id: s for s in load_scenarios(REPO / "scenarios")}
    suite = parse_suite({
        "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
        "loader_version": "0.16.5",
        "scenarios": [scenario],
        "variants": [{"name": "base", "mods": []}],
        "baseline": "base",
    })
    return Harness(
        suite, scenarios, work_dir=tmp_path, headlessmc=launcher, **extra
    ), scenarios


class TestProbe:
    def test_reads_the_flags_a_launcher_names(self, tmp_path):
        launcher = fake_launcher(tmp_path / "hmc", FULL_HELP)
        found = probe_launcher(launcher)
        assert found.probed
        assert found.accepts("--quickPlaySingleplayer")
        assert found.accepts("--loader-version")
        assert not found.unsupported_required

    def test_a_launcher_missing_a_required_flag_is_reported(self, tmp_path):
        launcher = fake_launcher(
            tmp_path / "hmc",
            "usage: launcher launch <version>\n  --loader <name>  mod loader\n",
        )
        found = probe_launcher(launcher)
        assert found.probed
        # No --gamedir, so the property dialect applies, and that dialect still
        # needs somewhere to put the JVM arguments.
        assert found.unsupported_required == ("--jvm",)
        assert not found.speaks_flags

    def test_no_gamedir_but_a_jvm_flag_is_the_property_dialect(self, tmp_path):
        """HeadlessMC 2.x, whose launch names only --jvm and --retries.

        Reporting --gamedir missing here blocked a launcher that works, because
        it takes the instance directory as a property instead.
        """
        launcher = fake_launcher(
            tmp_path / "hmc",
            "launch : Launches the game.\n  --jvm  Jvm args to use.\n"
            "  --retries  Retry count.\n",
        )
        found = probe_launcher(launcher)
        assert found.probed
        assert not found.speaks_flags
        assert not found.unsupported_required

    def test_help_naming_no_flags_at_all_is_treated_as_unreadable(self, tmp_path):
        """Indistinguishable from a launcher that would not print help."""
        launcher = fake_launcher(tmp_path / "hmc", "usage: launcher launch <version>")
        found = probe_launcher(launcher)
        assert not found.probed
        assert found.accepts("--gamedir")

    def test_unreadable_help_assumes_support_rather_than_absence(self, tmp_path):
        """Refusing to run because --help was unparseable is the worse failure."""
        launcher = tmp_path / "missing"
        found = probe_launcher(launcher)
        assert not found.probed
        assert found.accepts("--anything")


class TestLaunchCommand:
    def test_unsupported_optional_flags_are_dropped(self, tmp_path):
        launcher = fake_launcher(tmp_path / "hmc", MINIMAL_HELP)
        h, scenarios = harness(tmp_path / "w", launcher)
        command = h._launch_command(
            tmp_path, scenarios["entity-mobcap-saturation"], h.suite.variants[0]
        )
        assert "--loader" not in command
        assert "--loader-version" not in command
        # The required ones stay.
        assert "--gamedir" in command and "--jvm" in command

    def test_supported_flags_are_used(self, tmp_path):
        launcher = fake_launcher(tmp_path / "hmc", FULL_HELP)
        h, scenarios = harness(tmp_path / "w", launcher)
        command = h._launch_command(
            tmp_path, scenarios["entity-mobcap-saturation"], h.suite.variants[0]
        )
        assert "--loader" in command
        assert "--loader-version" in command

    def test_quick_play_falls_back_to_a_pass_through(self, tmp_path):
        """It is a vanilla game argument; a launcher may forward past `--`."""
        launcher = fake_launcher(tmp_path / "hmc", MINIMAL_HELP)
        scenarios = {s.id: s for s in load_scenarios(REPO / "scenarios")}
        client = next(s for s in scenarios.values() if s.side is Side.CLIENT)
        h, _ = harness(tmp_path / "w", launcher, scenario=client.id)

        command = h._launch_command(tmp_path, client, h.suite.variants[0])
        assert "--" in command
        assert command.index("--") < command.index("--quickPlaySingleplayer")

    def test_extra_arguments_are_appended_verbatim(self, tmp_path):
        launcher = fake_launcher(tmp_path / "hmc", FULL_HELP)
        h, scenarios = harness(
            tmp_path / "w", launcher, extra_launch_args=["--offline", "-q"]
        )
        command = h._launch_command(
            tmp_path, scenarios["entity-mobcap-saturation"], h.suite.variants[0]
        )
        assert command[-2:] == ["--offline", "-q"]


class TestPreflight:
    def test_a_launcher_missing_required_flags_blocks(self, tmp_path):
        launcher = fake_launcher(
            tmp_path / "hmc", "usage: launcher\n  --loader <name>  mod loader\n"
        )
        h, _ = harness(tmp_path / "w", launcher)
        result = h.preflight(require_account=False)
        check = next(c for c in result.checks if c.name == "launcher flags")
        assert check.severity is Severity.BLOCK
        assert "--jvm" in check.detail
        assert not result.admissible

    def test_the_property_dialect_is_not_reported_as_missing_flags(self, tmp_path):
        launcher = fake_launcher(
            tmp_path / "hmc",
            "launch : Launches the game.\n  --jvm  Jvm args to use.\n",
        )
        h, _ = harness(tmp_path / "w", launcher)
        check = next(
            c for c in h.preflight(require_account=False).checks
            if c.name == "launcher flags"
        )
        assert check.severity is Severity.OK
        assert "hmc.gamedir" in check.detail

    def test_dropped_optional_flags_are_reported(self, tmp_path):
        launcher = fake_launcher(tmp_path / "hmc", MINIMAL_HELP)
        h, _ = harness(tmp_path / "w", launcher)
        check = next(
            c for c in h.preflight(require_account=False).checks
            if c.name == "launcher flags"
        )
        assert check.severity is Severity.INFO
        assert "--loader" in check.detail

    def test_a_complete_launcher_passes(self, tmp_path):
        launcher = fake_launcher(tmp_path / "hmc", FULL_HELP)
        h, _ = harness(tmp_path / "w", launcher)
        check = next(
            c for c in h.preflight(require_account=False).checks
            if c.name == "launcher flags"
        )
        assert check.severity is Severity.OK

    def test_a_launcher_without_gamedir_is_driven_by_property(self, tmp_path):
        """HeadlessMC 2.x names neither the instance nor the loader on a flag.

        Its launch takes a version id and --jvm. The instance is hmc.gamedir,
        game arguments are hmc.gameargs, and the loader is chosen by asking for
        the loader's own version id. Emitting --gamedir at it silently launched
        vanilla in the wrong directory with none of the mods under test.
        """
        launcher = printing_stand_in(
            tmp_path / "hmc.jar",
            "launch : Launches the game.\n  --jvm  Jvm args to use.\n"
            "  --retries  How many times to retry.\n",
        )
        # The stand-in is not really a jar; name it one so the jar path is taken.
        jar = tmp_path / "renamed.jar"
        jar.write_bytes(launcher.read_bytes())

        h, scenarios = harness(tmp_path / "w", launcher, scenario="visual-biome-flyby")
        command = h._launch_command(
            tmp_path / "inst", scenarios["visual-biome-flyby"], h.suite.variants[0]
        )
        joined = " ".join(command)

        assert "--gamedir" not in joined
        assert f"-Dhmc.gamedir={tmp_path / 'inst'}" in command
        assert "--command" in command
        assert any(part.startswith("launch fabric-loader-0.16.5-1.21.1")
                   for part in command)
        assert any("quickPlaySingleplayer" in part and part.startswith("-Dhmc.gameargs")
                   for part in command)
        # -quit returns before the game does; the harness has to wait for it.
        assert "-quit" not in joined

        # JVM arguments go in the property, never inside the command string.
        # HeadlessMC splits that string on whitespace and --jvm takes one token,
        # so a second argument became a stray positional: the game launched at
        # the title screen with quick-play silently dropped.
        launch = next(p for p in command if p.startswith("launch "))
        assert "--jvm" not in launch
        assert any(p.startswith("-Dhmc.jvmargs=") for p in command)

    def test_a_server_ignores_the_launcher_entirely(self, tmp_path):
        """One suite can hold both sides: HeadlessMC for the client, a jar for
        the server. The property dialect runs the launcher from its own
        directory, because that is where it keeps the account it logged in
        with. A server launched directly has no launcher and no account, and
        running it from there would put its world beside the jar instead of in
        the instance the harness prepared.
        """
        launcher = printing_stand_in(
            tmp_path / "hmc.jar",
            "launch : Launches the game.\n  --jvm  Jvm args to use.\n",
        )
        server = tmp_path / "server.jar"
        server.write_bytes(b"PK\x03\x04")

        h, scenarios = harness(
            tmp_path / "w", launcher, scenario="entity-mobcap-saturation",
            server_jar=server, accept_eula=True,
        )
        scenario = scenarios["entity-mobcap-saturation"]
        instance = tmp_path / "inst"

        assert not h.launcher_capabilities().speaks_flags
        command = h._launch_command(instance, scenario, h.suite.variants[0])
        assert str(launcher) not in " ".join(command)
        assert command[-1] == "nogui"
        assert h._launch_cwd(instance, scenario) == instance

    def test_the_instance_path_is_absolute(self, tmp_path, monkeypatch):
        """The launcher runs from its own directory, not from the instance.

        A relative hmc.gamedir resolved against the launcher's directory
        instead. HeadlessMC did not complain: it created that directory, found
        no mods and no world in it, and left the game at the title screen, so
        every run failed with "probe output not found" and nothing said why.
        """
        monkeypatch.chdir(tmp_path)
        launcher = printing_stand_in(
            tmp_path / "hmc.jar", "launch : Launches the game.\n  --jvm  Jvm args.\n"
        )
        h, scenarios = harness(
            tmp_path / "w", launcher, scenario="visual-biome-flyby"
        )

        command = h._launch_command(
            Path("relative/instance"),
            scenarios["visual-biome-flyby"],
            h.suite.variants[0],
        )
        gamedir = next(p for p in command if p.startswith("-Dhmc.gamedir="))
        assert Path(gamedir.split("=", 1)[1]).is_absolute()

    def test_instance_directories_are_absolute(self, tmp_path, monkeypatch):
        """Every path that crosses into another process has to be absolute.

        The probe reads MCBENCH_PROBE_CONFIG from the environment and stays
        inert when it cannot find it, by design, because that is how it behaves
        in a normal play session. A relative path therefore produced a run that
        launched, played, and recorded nothing, with no error anywhere.
        """
        monkeypatch.chdir(tmp_path)
        launcher = printing_stand_in(tmp_path / "hmc", FULL_HELP)
        h, _ = harness(Path("relative-work"), launcher)

        planned = h.build_plan().runs[0]
        assert h._instance_dir(planned).is_absolute()

    def test_a_server_scenario_is_refused_rather_than_run_as_a_client(self, tmp_path):
        """The property dialect has no --server, and silence would be worse.

        Dropping it launches the client instead. The run then succeeds and
        reports frametimes for a scenario whose whole purpose was tick cost.
        """
        from mcbench.runner.harness import HarnessError

        launcher = printing_stand_in(
            tmp_path / "hmc.jar", "launch : Launches the game.\n  --jvm  Jvm args.\n"
        )
        h, scenarios = harness(tmp_path / "w", launcher)
        with pytest.raises(HarnessError, match="server scenario"):
            h._launch_command(
                tmp_path / "inst",
                scenarios["entity-mobcap-saturation"],
                h.suite.variants[0],
            )

    def test_jvm_arguments_survive_being_more_than_one(self, tmp_path):
        launcher = printing_stand_in(
            tmp_path / "hmc.jar", "launch : Launches the game.\n  --jvm  Jvm args.\n"
        )
        h, scenarios = harness(
            tmp_path / "w", launcher, scenario="visual-biome-flyby"
        )
        expected = h.effective_jvm_args(h.suite.variants[0])
        assert len(expected) > 1, "the fixture needs more than one argument to prove it"

        command = h._launch_command(
            tmp_path / "inst",
            scenarios["visual-biome-flyby"],
            h.suite.variants[0],
        )
        jvm = next(p for p in command if p.startswith("-Dhmc.jvmargs="))
        for argument in expected:
            assert argument in jvm

    def test_the_probe_runs_once_per_harness(self, tmp_path):
        launcher = fake_launcher(tmp_path / "hmc", FULL_HELP)
        h, _ = harness(tmp_path / "w", launcher)
        assert h.launcher_capabilities() is h.launcher_capabilities()


class TestAuthoredWorldIsTheOneUsed:
    """The level.dat the harness writes is a request, not a guarantee.

    A client that rejects it makes its own world with its own seed, and a run
    over that is a measurement of different terrain that looks perfectly normal.
    """

    def _client_harness(self, tmp_path):
        scenarios = {s.id: s for s in load_scenarios(REPO / "scenarios")}
        client = next(s for s in scenarios.values() if s.side is Side.CLIENT)
        h, _ = harness(
            tmp_path / "w",
            fake_launcher(tmp_path / "hmc", FULL_HELP),
            scenario=client.id,
        )
        return h, client

    def test_the_authored_world_passes(self, tmp_path):
        from mcbench.world import create_world

        h, client = self._client_harness(tmp_path)
        instance = tmp_path / "instance"
        world = create_world(
            instance / "saves", name=h._client_world_name(client), seed=client.seed
        )
        assert h._client_world_mismatch(instance, client, world) == ""

    def test_a_world_the_client_made_itself_is_rejected(self, tmp_path):
        from mcbench.world import create_world

        h, client = self._client_harness(tmp_path)
        instance = tmp_path / "instance"
        create_world(
            instance / "saves", name=h._client_world_name(client), seed=client.seed
        )
        stray = create_world(instance / "saves", name="New World", seed=999)
        assert "New World" in h._client_world_mismatch(instance, client, stray)

    def test_a_wrong_seed_is_rejected(self, tmp_path):
        from mcbench.world import create_world

        h, client = self._client_harness(tmp_path)
        instance = tmp_path / "instance"
        world = create_world(
            instance / "saves",
            name=h._client_world_name(client),
            seed=client.seed + 1,
        )
        assert "seed" in h._client_world_mismatch(instance, client, world)

    def test_an_unreadable_level_dat_is_rejected(self, tmp_path):
        h, client = self._client_harness(tmp_path)
        instance = tmp_path / "instance"
        world = instance / "saves" / h._client_world_name(client)
        world.mkdir(parents=True)
        (world / "level.dat").write_bytes(b"not nbt")
        assert "could not be read" in h._client_world_mismatch(instance, client, world)
