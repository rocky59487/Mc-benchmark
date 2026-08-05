"""Host facts, per operating system.

preflight.py decides what a reading means. This module decides how to obtain it,
so the checks themselves carry no platform logic.

Every probe returns ``None`` or an empty result when the platform cannot answer.
An unusual system degrades the report; it never stops the run.

Standard library only, which on Windows means ctypes and winreg rather than
WMI. The one probe that has no cheap native equivalent, enumerating process
command lines, shells out and degrades if that fails.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
from functools import lru_cache
from pathlib import Path

__all__ = [
    "available_memory_mb",
    "cpu_model",
    "cpu_scaling_profile",
    "display_description",
    "display_refresh_hz",
    "graphics_adapters",
    "graphics_driver_versions",
    "hypervisor",
    "power_source",
    "running_command_lines",
    "software_renderer_reason",
    "total_memory_mb",
]

WINDOWS = platform.system() == "Windows"
LINUX = platform.system() == "Linux"
MACOS = platform.system() == "Darwin"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Adapter names Windows reports when no hardware driver is bound. GL and Vulkan
#: fall back to CPU rasterisation on all of them.
_SOFTWARE_ADAPTERS = (
    "microsoft basic display",
    "microsoft basic render",
    "microsoft remote display",
    "microsoft hyper-v video",
    "warp",
    "llvmpipe",
    "softpipe",
    "swrast",
)

#: Windows power schemes that hold clocks up. The GUIDs are stable; the labels
#: beside them are localised, so matching on text would fail outside en-US.
_PINNED_SCHEMES = {
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "high performance",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "ultimate performance",
}

_VM_VENDORS = (
    "vmware", "virtualbox", "innotek", "qemu", "kvm", "xen", "parallels",
    "bochs", "bhyve", "hyper-v", "virtual machine",
)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _console_encoding() -> str:
    """What a console program's output is actually encoded in.

    Windows console tools write in the OEM code page, not the interpreter's
    preferred encoding, and text=True decodes with the latter. On a machine
    whose OEM page is not UTF-8 the two disagree and the difference reaches the
    results document: this recorded the active power scheme as
    ``power scheme: ???`` on a Traditional Chinese install, and by then the
    original bytes were gone.
    """
    if WINDOWS:
        try:
            import ctypes

            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            return "utf-8"
    return "utf-8"


def _run(command: list[str], *, timeout: float = 15.0) -> str | None:
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for encoding in (_console_encoding(), "utf-8"):
        try:
            return done.stdout.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    # Never lose the reading itself to an encoding problem; latin-1 always decodes.
    return done.stdout.decode("latin-1", "replace")


def _powershell(script: str) -> str | None:
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    )


def _registry_value(subkey: str, name: str) -> str | None:
    if not WINDOWS:
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value).strip() or None


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_memory_mb() -> tuple[int, int] | None:
    """(total, available) in MB from GlobalMemoryStatusEx."""
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    return status.ullTotalPhys // 1048576, status.ullAvailPhys // 1048576


def _meminfo_mb(field: str) -> int | None:
    text = _read("/proc/meminfo")
    if not text:
        return None
    match = re.search(rf"^{field}:\s+(\d+) kB", text, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def _vm_stat_available_mb() -> int | None:
    """Free plus reclaimable pages, which is what MemAvailable means on Linux."""
    text = _run(["vm_stat"])
    if not text:
        return None
    page_size = 4096
    if match := re.search(r"page size of (\d+) bytes", text):
        page_size = int(match.group(1))

    pages = 0
    found = False
    for kind in ("free", "inactive", "speculative"):
        if match := re.search(rf"^Pages {kind}:\s+(\d+)\.", text, re.MULTILINE):
            pages += int(match.group(1))
            found = True
    return pages * page_size // 1048576 if found else None


def total_memory_mb() -> int | None:
    if WINDOWS:
        readings = _windows_memory_mb()
        return readings[0] if readings else None
    if MACOS:
        text = _run(["sysctl", "-n", "hw.memsize"])
        return int(text.strip()) // 1048576 if text and text.strip().isdigit() else None
    return _meminfo_mb("MemTotal")


def available_memory_mb() -> int | None:
    if WINDOWS:
        readings = _windows_memory_mb()
        return readings[1] if readings else None
    if MACOS:
        return _vm_stat_available_mb()
    return _meminfo_mb("MemAvailable")


# --------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def cpu_model() -> str | None:
    if WINDOWS:
        return _registry_value(
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString"
        )
    if MACOS:
        text = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        return text.strip() if text and text.strip() else None
    cpuinfo = _read("/proc/cpuinfo") or ""
    match = re.search(r"model name\s+:\s+(.+)", cpuinfo)
    return match.group(1).strip() if match else None


@lru_cache(maxsize=1)
def cpu_scaling_profile() -> tuple[bool | None, str]:
    """Whether clocks are configured to stay put, and what was read.

    ``True`` means the machine is set to hold frequency, ``False`` that it may
    drift between runs, ``None`` that the platform exposes no such setting.
    """
    if WINDOWS:
        return _windows_power_scheme()
    if LINUX:
        governors = sorted(
            Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")
        )
        values = {v for p in governors if (v := _read(str(p)))}
        if not values:
            return None, "no cpufreq interface (typical in containers and VMs)"
        listed = ", ".join(sorted(values))
        return values <= {"performance"}, f"governor(s): {listed}"
    return None, "no frequency governor interface on this platform"


def _windows_power_scheme() -> tuple[bool | None, str]:
    output = _run(["powercfg", "/getactivescheme"])
    if not output:
        return None, "could not read the active power scheme"

    match = re.search(r"([0-9a-fA-F-]{36})", output)
    if not match:
        return None, "could not read the active power scheme"

    guid = match.group(1).lower()
    label = ""
    if named := re.search(r"\(([^)]+)\)", output):
        label = named.group(1).strip()

    if guid in _PINNED_SCHEMES:
        return True, f"power scheme: {label or _PINNED_SCHEMES[guid]}"
    return False, f"power scheme: {label or guid}"


# --------------------------------------------------------------------------
# Graphics and display
# --------------------------------------------------------------------------


class _DisplayDeviceW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_ulong),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


def _windows_adapters() -> list[str]:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError):
        return []

    enumerate_devices = user32.EnumDisplayDevicesW
    enumerate_devices.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DisplayDeviceW),
        ctypes.c_ulong,
    ]
    enumerate_devices.restype = ctypes.c_int

    names: list[str] = []
    index = 0
    while True:
        device = _DisplayDeviceW()
        device.cb = ctypes.sizeof(device)
        if not enumerate_devices(None, index, ctypes.byref(device), 0):
            break
        name = device.DeviceString.strip()
        if name and name not in names:
            names.append(name)
        index += 1
        if index > 32:
            break
    return names


@lru_cache(maxsize=1)
def graphics_adapters() -> tuple[str, ...]:
    """Names identifying the graphics hardware, empty when none is bound.

    Windows reports adapter model names. Linux reports DRM node names from
    /dev/dri, which exist only when a driver is bound and are absent in a plain
    container.
    """
    if WINDOWS:
        return tuple(_windows_adapters())
    if MACOS:
        return ("integrated",)
    dri = Path("/dev/dri")
    if not dri.is_dir():
        return ()
    try:
        nodes = sorted(p.name for p in dri.iterdir())
    except OSError:
        return ()
    return tuple(n for n in nodes if n.startswith(("render", "card")))


class _DevModeW(ctypes.Structure):
    """Only the head of DEVMODEW, up to the field being read.

    The tail is padded to the real size so EnumDisplaySettingsW writes inside
    the allocation. Declaring the whole structure would mean tracking a layout
    that has grown across Windows versions, to read one field near its front.
    """

    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("_position", ctypes.c_byte * 16),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("_tail", ctypes.c_byte * 64),
    ]


def _windows_refresh_hz() -> int | None:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    mode = _DevModeW()
    mode.dmSize = ctypes.sizeof(_DevModeW)
    # -1 is ENUM_CURRENT_SETTINGS: what the display is doing now, rather than a
    # mode it merely supports.
    if not user32.EnumDisplaySettingsW(None, -1, ctypes.byref(mode)):
        return None
    hz = int(mode.dmDisplayFrequency)
    # 0 and 1 are documented as meaning "the hardware default", which is not a
    # rate and must not be recorded as one.
    return hz if hz > 1 else None


@lru_cache(maxsize=1)
def display_refresh_hz() -> int | None:
    """Refresh rate of the primary display, or None when it cannot be read.

    Recorded because METHODOLOGY section 7 promises it and because a frametime
    distribution pinned to a refresh interval is measuring the display rather
    than the mod. ``vsync_suspected`` infers a rate from the samples; having the
    real one turns that inference into something checkable.
    """
    if WINDOWS:
        return _windows_refresh_hz()
    # X11 reports it through xrandr, which needs a display to talk to; anywhere
    # headless there is nothing to report and None is the honest answer.
    output = _run(["xrandr", "--current"]) if not MACOS else None
    if not output:
        return None
    for line in output.splitlines():
        if "*" not in line:
            continue
        for token in line.split():
            if token.endswith(("*", "*+")):
                try:
                    return round(float(token.rstrip("*+")))
                except ValueError:
                    return None
    return None


#: Where Windows keeps one subkey per display adapter.
_DISPLAY_CLASS = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
)


@lru_cache(maxsize=1)
def graphics_driver_versions() -> tuple[str, ...]:
    """``adapter=driver`` for each display adapter, in registry order.

    Recorded because METHODOLOGY section 7 promises it and because a driver
    revision moves rendering numbers on its own; two results from the same GPU
    and different drivers are not the same measurement.

    Read from the registry rather than WMI to keep the module on the standard
    library and off a subprocess.
    """
    if not WINDOWS:
        return ()
    import winreg

    found: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as parent:
            for index in range(16):
                try:
                    name = winreg.EnumKey(parent, index)
                except OSError:
                    break
                if not name.isdigit():
                    continue
                adapter = _registry_value(
                    f"{_DISPLAY_CLASS}\\{name}", "DriverDesc"
                )
                version = _registry_value(
                    f"{_DISPLAY_CLASS}\\{name}", "DriverVersion"
                )
                if adapter and version:
                    found.append(f"{adapter}={version}")
    except OSError:
        return ()
    return tuple(found)


def software_renderer_reason() -> str | None:
    """Why rendering would land on the CPU, or None when it would not."""
    forced = os.environ.get("LIBGL_ALWAYS_SOFTWARE", "")
    driver = os.environ.get("MESA_LOADER_DRIVER_OVERRIDE", "")
    if forced not in ("", "0", "false"):
        return f"LIBGL_ALWAYS_SOFTWARE={forced!r}"
    if driver in ("llvmpipe", "swrast", "softpipe"):
        return f"MESA_LOADER_DRIVER_OVERRIDE={driver!r}"

    adapters = graphics_adapters()
    if adapters and all(
        any(marker in name.lower() for marker in _SOFTWARE_ADAPTERS)
        for name in adapters
    ):
        return f"only software adapters present ({', '.join(adapters)})"
    return None


def display_description() -> str | None:
    """A description of the attached display, or None when there is none."""
    if WINDOWS:
        try:
            monitors = ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS
        except (AttributeError, OSError):
            return None
        return f"{monitors} monitor(s)" if monitors > 0 else None
    if MACOS:
        return "quartz"
    for name in ("WAYLAND_DISPLAY", "DISPLAY"):
        if value := os.environ.get(name):
            return f"{name}={value}"
    return None


# --------------------------------------------------------------------------
# Power, processes, virtualisation
# --------------------------------------------------------------------------


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def power_source() -> str | None:
    """``"ac"``, ``"battery"``, or None when the machine has no battery."""
    if WINDOWS:
        status = _SystemPowerStatus()
        try:
            ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        except (AttributeError, OSError):
            return None
        if not ok or status.BatteryFlag == 128:
            return None
        if status.ACLineStatus == 1:
            return "ac"
        if status.ACLineStatus == 0:
            return "battery"
        return None

    if LINUX:
        for supply in sorted(Path("/sys/class/power_supply").glob("*")):
            if _read(str(supply / "type")) != "Mains":
                continue
            return "battery" if _read(str(supply / "online")) == "0" else "ac"
        return None

    if MACOS:
        text = _run(["pmset", "-g", "batt"]) or ""
        if "Battery Power" in text:
            return "battery"
        if "AC Power" in text:
            return "ac"
    return None


def running_command_lines() -> list[str] | None:
    """Every process command line, or None when they cannot be enumerated.

    Windows needs the command line rather than the image name: Minecraft runs
    as javaw.exe, so the loader and version live entirely in the arguments.
    """
    if WINDOWS:
        output = _powershell(
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"
        )
    else:
        output = _run(["ps", "-eo", "pid,args"])
    if output is None:
        return None
    return [line.strip() for line in output.splitlines() if line.strip()]


@lru_cache(maxsize=1)
def hypervisor() -> str | None:
    """The virtualisation platform in use, or None on bare metal.

    Windows is identified by the firmware's reported system model. Reading
    HypervisorPresent instead would flag every machine with virtualisation-based
    security enabled, which on Windows 11 is most of them.
    """
    if WINDOWS:
        bios = r"HARDWARE\DESCRIPTION\System\BIOS"
        fields = [
            _registry_value(bios, "SystemManufacturer"),
            _registry_value(bios, "SystemProductName"),
        ]
        haystack = " ".join(f for f in fields if f).lower()
        for vendor in _VM_VENDORS:
            if vendor in haystack:
                return vendor
        return None

    detected = _run(["systemd-detect-virt"])
    if detected and detected.strip() and detected.strip() != "none":
        return detected.strip()
    if Path("/.dockerenv").exists():
        return "docker"
    return None
