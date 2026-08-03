"""What the installed launcher actually accepts.

The harness builds a HeadlessMC command line, and every flag on it is an
assumption about a tool it does not ship. Those assumptions used to be made
silently: an unrecognised flag surfaces as a failed launch minutes in, repeated
once per planned run, with an error that names nothing useful.

So the launcher is asked what it supports before the suite starts. Flags it does
not recognise are either dropped — when the run can proceed without them — or
reported as a preflight blocker naming the flag. A capability that cannot be
probed is assumed present rather than absent: refusing to run because ``--help``
was unparseable would be a worse failure than trying.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["LauncherCapabilities", "probe_launcher"]

#: Flags the harness may put on a launch command, and what each is for.
#:
#: ``required`` flags cannot be dropped: without them the run measures the wrong
#: thing or nothing at all. The rest degrade with a warning.
KNOWN_FLAGS = {
    "--gamedir": "point the instance at its own directory",
    "--jvm": "pass JVM arguments, including heap and the probe agent",
    "--loader": "select the mod loader",
    "--loader-version": "pin the loader build",
    "--quickPlaySingleplayer": "enter the scenario world directly",
    "--server": "launch the dedicated server rather than the client",
}

REQUIRED_FLAGS = frozenset({"--gamedir", "--jvm"})

_FLAG = re.compile(r"--[A-Za-z][\w-]*")


@dataclass(frozen=True)
class LauncherCapabilities:
    """Which flags the installed launcher recognises."""

    path: Path | None
    supported: frozenset[str] = frozenset()
    probed: bool = False
    """False when help output could not be obtained; every flag is then assumed
    supported rather than assumed missing."""
    detail: str = ""
    unsupported_required: tuple[str, ...] = field(default=())

    def accepts(self, flag: str) -> bool:
        return not self.probed or flag in self.supported

    def missing(self, flags: frozenset[str]) -> tuple[str, ...]:
        if not self.probed:
            return ()
        return tuple(sorted(f for f in flags if f not in self.supported))


def probe_launcher(
    launcher: Path | None, *, timeout_s: float = 30.0
) -> LauncherCapabilities:
    """Ask the launcher for its help text and collect the flags it names.

    Every documented HeadlessMC-style launcher prints its options on ``--help``,
    and scraping them is the only portable way to tell one build from another.
    """
    if launcher is None:
        return LauncherCapabilities(path=None, detail="no launcher configured")

    base = (
        ["java", "-jar", str(launcher)]
        if str(launcher).endswith(".jar")
        else [str(launcher)]
    )

    found: set[str] = set()
    errors: list[str] = []
    for args in (["--help"], ["launch", "--help"], ["help", "launch"]):
        try:
            completed = subprocess.run(
                [*base, *args], capture_output=True, text=True,
                timeout=timeout_s, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{' '.join(args)}: {exc}")
            continue
        found.update(_FLAG.findall(completed.stdout or ""))
        found.update(_FLAG.findall(completed.stderr or ""))

    if not found:
        return LauncherCapabilities(
            path=launcher,
            probed=False,
            detail="; ".join(errors) or "help output named no flags",
        )

    return LauncherCapabilities(
        path=launcher,
        supported=frozenset(found),
        probed=True,
        detail=f"{len(found)} flag(s) recognised",
        unsupported_required=tuple(
            sorted(f for f in REQUIRED_FLAGS if f not in found)
        ),
    )
