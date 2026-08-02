"""Environment capability and quiescence checks.

Implements docs/METHODOLOGY.md section 3. This runs before any measurement and
decides whether the machine can produce a number worth publishing.

The most valuable thing this module does is *refuse*. A benchmark that always
produces output trains people to trust output it should never have produced —
and the single easiest way to publish a meaningless Minecraft number is to
benchmark a GPU renderer on a software rasteriser, where the GPU work the mod
exists to optimise never happens at all.

Standard library only; every probe degrades to UNKNOWN rather than raising, so a
missing /proc entry or an unusual platform never takes the harness down.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

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
        return self.admissible and not self.warnings


# --------------------------------------------------------------------------
# Individual probes
# --------------------------------------------------------------------------


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _check_gpu(*, needs_gpu: bool) -> Check:
    """Detect real graphics hardware.

    On Linux, kernel DRM render nodes under /dev/dri are the reliable signal:
    they exist when a GPU driver is bound, and are absent in a plain container.
    Their absence means any GL context will fall back to a software rasteriser.

    For a client scenario this is a hard blocker, not a warning. Software
    rasterisation does not merely make rendering slower — it relocates the work.
    A renderer optimises GPU-side draw submission, culling, and buffer
    management; run it on llvmpipe and that work becomes CPU work with entirely
    different bottlenecks. The resulting number is not a slow measurement of the
    mod, it is a measurement of something else.
    """
    dri = Path("/dev/dri")
    nodes = sorted(p.name for p in dri.iterdir()) if dri.is_dir() else []
    has_render_node = any(n.startswith(("render", "card")) for n in nodes)

    if has_render_node:
        return Check("gpu", Severity.OK, f"graphics device present ({', '.join(nodes)})")

    if not needs_gpu:
        return Check(
            "gpu",
            Severity.INFO,
            "no graphics device; acceptable because this run is server-side only",
        )

    return Check(
        "gpu",
        Severity.BLOCK,
        "no graphics device found (/dev/dri absent) — any GL context would fall "
        "back to software rasterisation",
        remedy=(
            "Client-side rendering cannot be measured meaningfully without a GPU. "
            "Software rasterisation moves GPU work onto the CPU, so the result "
            "would describe a different bottleneck than any real user has. Run "
            "client scenarios on hardware with a GPU, or restrict this suite to "
            "server-side scenarios with --side server."
        ),
    )


def _check_software_rasteriser(*, needs_gpu: bool) -> Check:
    """Detect an active software GL driver even when a device exists.

    Belt and braces alongside the GPU check: LIBGL_ALWAYS_SOFTWARE or a
    llvmpipe/swrast selection forces software rendering regardless of hardware.
    """
    forced = os.environ.get("LIBGL_ALWAYS_SOFTWARE", "")
    driver = os.environ.get("MESA_LOADER_DRIVER_OVERRIDE", "")

    if forced not in ("", "0", "false") or driver in ("llvmpipe", "swrast", "softpipe"):
        severity = Severity.BLOCK if needs_gpu else Severity.INFO
        return Check(
            "software_rasteriser",
            severity,
            f"software rendering forced (LIBGL_ALWAYS_SOFTWARE={forced!r}, "
            f"driver={driver!r})",
            remedy="Unset LIBGL_ALWAYS_SOFTWARE and MESA_LOADER_DRIVER_OVERRIDE."
            if needs_gpu else "",
        )
    return Check("software_rasteriser", Severity.OK, "no forced software rendering")


def _check_display(*, needs_gpu: bool) -> Check:
    """A client run needs a display, real or virtual."""
    if not needs_gpu:
        return Check("display", Severity.INFO, "not required for a server-side run")
    if os.environ.get("DISPLAY"):
        return Check("display", Severity.OK, f"DISPLAY={os.environ['DISPLAY']}")
    if shutil.which("Xvfb"):
        return Check(
            "display",
            Severity.INFO,
            "no DISPLAY set, but Xvfb is available and will be started",
        )
    return Check(
        "display",
        Severity.BLOCK,
        "no DISPLAY and no Xvfb binary",
        remedy="Install Xvfb, or run with a real display attached.",
    )


def _check_competing_minecraft() -> Check:
    """Refuse to measure while another Minecraft process is running.

    A second JVM competing for CPU and memory is the most common way an
    otherwise careful benchmark gets silently ruined.
    """
    try:
        output = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return Check("competing_processes", Severity.WARN,
                     "could not enumerate processes")

    ours = str(os.getpid())
    hits = [
        line.strip() for line in output.splitlines()
        if ("net.minecraft" in line or "net.fabricmc" in line
            or "minecraftforge" in line or "neoforged" in line)
        and not line.strip().startswith(ours)
        and "ps -eo" not in line
    ]
    if hits:
        return Check(
            "competing_processes",
            Severity.BLOCK,
            f"{len(hits)} other Minecraft process(es) running",
            remedy="Stop other Minecraft instances before benchmarking.",
        )
    return Check("competing_processes", Severity.OK, "no competing Minecraft processes")


def _check_cpu_scaling() -> Check:
    """Frequency scaling lets the CPU speed drift between runs.

    Interleaved ordering (planner.py) converts this from a systematic bias into
    noise, so it warns rather than blocks — but a 'performance' governor makes
    intervals materially tighter and is worth telling the operator about.
    """
    governors = sorted(
        Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")
    )
    if not governors:
        return Check("cpu_scaling", Severity.INFO,
                     "no cpufreq interface (typical in containers and VMs)")

    values = {v for p in governors if (v := _read(str(p)))}
    if values <= {"performance"}:
        return Check("cpu_scaling", Severity.OK, "governor: performance")
    return Check(
        "cpu_scaling",
        Severity.WARN,
        f"governor(s): {', '.join(sorted(values))} — clock speed may drift between runs",
        remedy="sudo cpupower frequency-set -g performance",
    )


def _check_battery() -> Check:
    """Laptops on battery throttle aggressively and unpredictably."""
    for supply in sorted(Path("/sys/class/power_supply").glob("*")):
        if _read(str(supply / "type")) != "Mains":
            continue
        online = _read(str(supply / "online"))
        if online == "0":
            return Check(
                "power", Severity.WARN, "running on battery — expect aggressive throttling",
                remedy="Connect AC power before benchmarking.",
            )
        return Check("power", Severity.OK, "on AC power")
    return Check("power", Severity.INFO, "no battery detected")


def _check_memory(*, heap_mb: int) -> Check:
    """The heap plus headroom must fit, or GC behaviour dominates the result."""
    meminfo = _read("/proc/meminfo")
    if not meminfo:
        return Check("memory", Severity.INFO, "could not read /proc/meminfo")

    match = re.search(r"MemAvailable:\s+(\d+) kB", meminfo)
    if not match:
        return Check("memory", Severity.INFO, "MemAvailable not reported")

    available_mb = int(match.group(1)) // 1024
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
    hypervisor = None
    if shutil.which("systemd-detect-virt"):
        try:
            result = subprocess.run(
                ["systemd-detect-virt"], capture_output=True, text=True,
                timeout=5, check=False,
            )
            detected = result.stdout.strip()
            if detected and detected != "none":
                hypervisor = detected
        except (OSError, subprocess.SubprocessError):
            pass

    if hypervisor is None and Path("/.dockerenv").exists():
        hypervisor = "docker"

    if hypervisor:
        return Check(
            "virtualisation", Severity.WARN,
            f"running under {hypervisor} — timing may be less stable and CPU "
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

    cpuinfo = _read("/proc/cpuinfo") or ""
    if match := re.search(r"model name\s+:\s+(.+)", cpuinfo):
        host["cpu_model"] = match.group(1).strip()

    meminfo = _read("/proc/meminfo") or ""
    if match := re.search(r"MemTotal:\s+(\d+) kB", meminfo):
        host["memory_gb"] = str(int(match.group(1)) // 1024 // 1024)

    dri = Path("/dev/dri")
    host["gpu_nodes"] = (
        ",".join(sorted(p.name for p in dri.iterdir())) if dri.is_dir() else "none"
    )
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
