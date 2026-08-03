"""Environment capability and quiescence checks.

Implements docs/METHODOLOGY.md section 3. Runs before any measurement and
decides whether the machine can produce a number worth publishing.

Checks may block. Benchmarking a GPU renderer on a software rasteriser is the
easiest way to publish a meaningless Minecraft number: the GPU work the mod
exists to optimise never happens at all.

Platform readings come from hostinfo, which degrades rather than raising, so an
unfamiliar system produces a weaker report instead of stopping the run.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import hostinfo

__all__ = [
    "Severity",
    "Check",
    "Preflight",
    "run_preflight",
    "describe_host",
]


class Severity(str, Enum):
    """How a failed check affects admissibility."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    """Measurement proceeds; the result is flagged and excluded from the corpus."""
    BLOCK = "block"
    """Measurement must not proceed — any number produced would be misleading."""


@dataclass(frozen=True)
class Check:
    name: str
    severity: Severity
    detail: str
    remedy: str = ""

    @property
    def passed(self) -> bool:
        return self.severity in (Severity.OK, Severity.INFO)


@dataclass
class Preflight:
    """Outcome of all environment checks."""

    checks: list[Check] = field(default_factory=list)
    host: dict[str, str] = field(default_factory=dict)
    forced: bool = False
    """True when the operator overrode a blocker with ``--force``. Results from a
    forced run must never enter the corpus, and the override travels with them so
    that fact cannot be lost between the run and whoever reads it."""

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.severity is Severity.WARN]

    @property
    def admissible(self) -> bool:
        """Whether measurement may proceed at all."""
        return not self.blockers

    @property
    def publishable(self) -> bool:
        """Whether results may enter the shared corpus."""
        return self.admissible and not self.warnings and not self.forced

    def to_dict(self) -> dict:
        """The whole preflight state, for the results bundle.

        Every check with its severity, what was actually read, and the remedy —
        not just the publishable verdict. Collapsing this to one boolean threw
        away the readings a reader needs to judge whether two results are
        comparable: two runs on machines differing in CPU scaling governor,
        virtualisation, and free memory both serialised as ``publishable:
        False`` and looked equivalent.
        """
        return {
            "admissible": self.admissible,
            "publishable": self.publishable,
            "forced": self.forced,
            "host": dict(self.host),
            "checks": [
                {
                    "name": c.name,
                    "severity": c.severity.value,
                    "detail": c.detail,
                    "remedy": c.remedy,
                }
                for c in self.checks
            ],
            "blockers": [c.name for c in self.blockers],
            "warnings": [c.name for c in self.warnings],
        }


# --------------------------------------------------------------------------
# Individual probes
# --------------------------------------------------------------------------


def _check_gpu(*, needs_gpu: bool) -> Check:
    """Detect real graphics hardware.

    For a client scenario an absent GPU is a hard blocker. Software
    rasterisation relocates the work rather than merely slowing it: a renderer
    optimises GPU-side draw submission, culling and buffer management, and on
    llvmpipe all of that becomes CPU work with different bottlenecks. The number
    would describe something other than the mod.
    """
    adapters = hostinfo.graphics_adapters()
    if adapters:
        return Check(
            "gpu", Severity.OK, f"graphics device present ({', '.join(adapters)})"
        )

    if not needs_gpu:
        return Check(
            "gpu",
            Severity.INFO,
            "no graphics device; acceptable because this run is server-side only",
        )

    return Check(
        "gpu",
        Severity.BLOCK,
        "no graphics device found; any GL context would fall back to software "
        "rasterisation",
        remedy=(
            "Client-side rendering cannot be measured meaningfully without a GPU. "
            "Software rasterisation moves GPU work onto the CPU, so the result "
            "would describe a different bottleneck than any real user has. Run "
            "client scenarios on hardware with a GPU, or restrict this suite to "
            "server-side scenarios with --side server."
        ),
    )


def _check_software_rasteriser(*, needs_gpu: bool) -> Check:
    """Detect software rendering even when a device is present.

    A Mesa override forces it regardless of hardware, and Windows reports a
    basic display adapter when no vendor driver is bound.
    """
    reason = hostinfo.software_renderer_reason()
    if reason is None:
        return Check("software_rasteriser", Severity.OK, "no forced software rendering")

    return Check(
        "software_rasteriser",
        Severity.BLOCK if needs_gpu else Severity.INFO,
        f"software rendering in effect: {reason}",
        remedy=(
            "Unset LIBGL_ALWAYS_SOFTWARE and MESA_LOADER_DRIVER_OVERRIDE, or "
            "install the vendor graphics driver."
        )
        if needs_gpu
        else "",
    )


def _check_vsync(*, needs_gpu: bool) -> Check:
    """Detect a compositor or driver forcing vsync.

    mcbench writes ``enableVsync:false`` into every client instance, so the game
    will not ask for it. But a compositor or a driver-level override can impose
    it anyway, and then frametimes quantise to the refresh interval: every
    variant scores the same number and the benchmark measures the monitor
    instead of the renderer.

    This cannot be detected reliably before the run — the honest check is on the
    *data*, where a frametime distribution pinned to a refresh interval is
    unmistakable (see ``vsync_suspected`` in metrics). What preflight can do is
    flag the environment variables that are known to force it.
    """
    if not needs_gpu:
        return Check("vsync", Severity.INFO, "not applicable to a server-side run")

    forced = {
        name: os.environ[name]
        for name in ("__GL_SYNC_TO_VBLANK", "vblank_mode", "MESA_VK_WSI_PRESENT_MODE")
        if name in os.environ
    }
    # vblank_mode=0 and __GL_SYNC_TO_VBLANK=0 *disable* vsync, which is what we
    # want; only non-zero settings are a problem.
    forcing = {k: v for k, v in forced.items() if v not in ("0", "immediate")}
    if forcing:
        return Check(
            "vsync", Severity.WARN,
            "environment forces vsync: "
            + ", ".join(f"{k}={v}" for k, v in sorted(forcing.items())),
            remedy=(
                "Frametimes pinned to the refresh interval measure the display, "
                "not the renderer, and every variant scores the same. Set "
                "vblank_mode=0 (Mesa) or __GL_SYNC_TO_VBLANK=0 (NVIDIA)."
            ),
        )
    return Check(
        "vsync", Severity.OK,
        "no environment-level vsync override; the run is also checked for it",
    )


def _check_display(*, needs_gpu: bool) -> Check:
    """A client run needs a display, real or virtual."""
    if not needs_gpu:
        return Check("display", Severity.INFO, "not required for a server-side run")
    if display := hostinfo.display_description():
        return Check("display", Severity.OK, display)
    if shutil.which("Xvfb"):
        return Check(
            "display",
            Severity.INFO,
            "no display attached, but Xvfb is available and will be started",
        )
    return Check(
        "display",
        Severity.BLOCK,
        "no display attached and no Xvfb binary",
        remedy="Install Xvfb, or run with a real display attached.",
    )


#: Substrings that identify a Minecraft JVM in a process command line. The
#: launcher and the game both run as a bare `java`, so the loader packages are
#: the only reliable marker.
_MINECRAFT_MARKERS = ("net.minecraft", "net.fabricmc", "minecraftforge", "neoforged")


def _check_competing_minecraft() -> Check:
    """Refuse to measure while another Minecraft process is running.

    A second JVM competing for CPU and memory is the most common way an
    otherwise careful benchmark gets silently ruined.
    """
    lines = hostinfo.running_command_lines()
    if lines is None:
        return Check(
            "competing_processes", Severity.WARN, "could not enumerate processes"
        )

    ours = str(os.getpid())
    hits = [
        line
        for line in lines
        if any(marker in line for marker in _MINECRAFT_MARKERS)
        and line.split(maxsplit=1)[0] != ours
    ]
    if hits:
        return Check(
            "competing_processes",
            Severity.BLOCK,
            f"{len(hits)} other Minecraft process(es) running",
            remedy="Stop other Minecraft instances before benchmarking.",
        )
    return Check("competing_processes", Severity.OK, "no competing Minecraft processes")


#: Restores a fixed clock. The Windows GUID is the built-in High performance
#: scheme, whose display name is localised and so cannot be passed by name.
_PIN_CLOCK_REMEDY = (
    "powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
    if hostinfo.WINDOWS
    else "sudo cpupower frequency-set -g performance"
)


def _check_cpu_scaling() -> Check:
    """Frequency scaling lets the CPU speed drift between runs.

    Interleaved ordering (planner.py) turns this from a systematic bias into
    noise, so it warns rather than blocks. Pinning the clock still tightens
    intervals materially and is worth telling the operator about.
    """
    pinned, detail = hostinfo.cpu_scaling_profile()
    if pinned is None:
        return Check("cpu_scaling", Severity.INFO, detail)
    if pinned:
        return Check("cpu_scaling", Severity.OK, detail)
    return Check(
        "cpu_scaling",
        Severity.WARN,
        f"{detail}; clock speed may drift between runs",
        remedy=_PIN_CLOCK_REMEDY,
    )


def _check_battery() -> Check:
    """Laptops on battery throttle aggressively and unpredictably."""
    source = hostinfo.power_source()
    if source == "battery":
        return Check(
            "power",
            Severity.WARN,
            "running on battery; expect aggressive throttling",
            remedy="Connect AC power before benchmarking.",
        )
    if source == "ac":
        return Check("power", Severity.OK, "on AC power")
    return Check("power", Severity.INFO, "no battery detected")


def _check_memory(*, heap_mb: int) -> Check:
    """The heap plus headroom must fit, or GC behaviour dominates the result."""
    available_mb = hostinfo.available_memory_mb()
    if available_mb is None:
        return Check("memory", Severity.INFO, "could not read available memory")

    # The JVM needs the heap plus metaspace, code cache, native buffers and OS
    # page cache; a heap sized to exactly free memory swaps and the numbers
    # become meaningless.
    required_mb = int(heap_mb * 1.5) + 1024
    if available_mb < heap_mb:
        return Check(
            "memory", Severity.BLOCK,
            f"{available_mb} MB available, heap alone is {heap_mb} MB",
            remedy=f"Lower heap_mb, or free at least {required_mb} MB.",
        )
    if available_mb < required_mb:
        return Check(
            "memory", Severity.WARN,
            f"{available_mb} MB available; {required_mb} MB recommended for a "
            f"{heap_mb} MB heap",
            remedy="Close other applications, or lower heap_mb.",
        )
    return Check("memory", Severity.OK, f"{available_mb} MB available")


def _check_cpu_count(*, minimum: int = 4) -> Check:
    count = os.cpu_count() or 0
    if count < minimum:
        return Check(
            "cpu_count", Severity.WARN,
            f"{count} logical CPU(s); Minecraft's worker pools are starved below {minimum}",
        )
    return Check("cpu_count", Severity.OK, f"{count} logical CPUs")


def _check_virtualisation() -> Check:
    """Virtual machines can have unstable timers and noisy neighbours."""
    platform_name = hostinfo.hypervisor()
    if platform_name:
        return Check(
            "virtualisation",
            Severity.WARN,
            f"running under {platform_name}; timing may be less stable and CPU "
            f"shared with other tenants",
            remedy="Prefer bare metal for published results.",
        )
    return Check("virtualisation", Severity.OK, "bare metal")


def _check_disk(*, path: str = ".", required_gb: int = 12) -> Check:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return Check("disk", Severity.INFO, f"could not stat {path}: {exc}")

    free_gb = usage.free // (1024 ** 3)
    if free_gb < required_gb:
        return Check(
            "disk", Severity.BLOCK,
            f"{free_gb} GB free, {required_gb} GB needed for instances and worlds",
            remedy="Free disk space, or point the work directory elsewhere.",
        )
    return Check("disk", Severity.OK, f"{free_gb} GB free")


def _check_account() -> Check:
    """A licensed account is required; the EULA has no exception for benchmarks.

    mcbench never handles credentials itself. HeadlessMC owns authentication and
    validates ownership, so this only reports whether it has a usable session.
    """
    candidates = [
        Path.home() / ".headlessmc" / "accounts.json",
        Path.home() / ".minecraft" / "launcher_accounts.json",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 2:
            return Check("account", Severity.OK, f"credentials present ({candidate})")

    return Check(
        "account", Severity.BLOCK,
        "no Minecraft account configured",
        remedy=(
            "Authenticate with HeadlessMC ('hmc login'). mcbench never stores or "
            "handles credentials itself. A licensed account is required by the "
            "Minecraft EULA; there is no benchmarking exemption."
        ),
    )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def describe_host() -> dict[str, str]:
    """Host facts recorded in every result's provenance."""
    host = {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": str(os.cpu_count() or 0),
    }

    if model := hostinfo.cpu_model():
        host["cpu_model"] = model
    if total_mb := hostinfo.total_memory_mb():
        host["memory_gb"] = str(total_mb // 1024)

    adapters = hostinfo.graphics_adapters()
    host["gpu"] = ",".join(adapters) if adapters else "none"
    return host


def run_preflight(
    *,
    needs_gpu: bool = True,
    heap_mb: int = 4096,
    require_account: bool = True,
    work_dir: str = ".",
) -> Preflight:
    """Run every environment check.

    Args:
        needs_gpu: True when the suite contains client-side scenarios. Server
            scenarios do no rendering, so the GPU checks relax to informational.
        heap_mb: Configured JVM heap, used to size the memory requirement.
        require_account: Set False to validate the environment without demanding
            credentials — useful for inspecting a machine before setting it up.
    """
    checks = [
        _check_gpu(needs_gpu=needs_gpu),
        _check_software_rasteriser(needs_gpu=needs_gpu),
        _check_display(needs_gpu=needs_gpu),
        _check_vsync(needs_gpu=needs_gpu),
        _check_competing_minecraft(),
        _check_cpu_count(),
        _check_cpu_scaling(),
        _check_battery(),
        _check_memory(heap_mb=heap_mb),
        _check_disk(path=work_dir),
        _check_virtualisation(),
    ]
    if require_account:
        checks.append(_check_account())

    return Preflight(checks=checks, host=describe_host())
