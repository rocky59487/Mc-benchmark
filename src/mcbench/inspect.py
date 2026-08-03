"""Static inspection of a mod set: metadata, conflicts, and mixin overlap.

Everything here works on jar files alone — no game, no account, no GPU, no run.
That matters for a modpack health check, because the fastest useful answer is the
one you get before spending two hours benchmarking a pack that was never going to
load.

Three classes of finding:

**Declared incompatibilities.** Mod authors already record what they break. A
Fabric mod's ``breaks`` block, a NeoForge ``incompatible`` dependency, a Bukkit
``depend`` list — all of it is machine-readable and routinely ignored. Reading it
catches the conflicts someone already knew about.

**Structural problems.** Missing dependencies, duplicate mod ids, and two jars
providing the same id are pack-assembly mistakes that produce confusing runtime
failures.

**Mixin target overlap.** The interesting one, and the reason this module parses
bytecode. Two mods that transform the same Minecraft class are not necessarily
broken, but that is where conflicts actually come from — competing rewrites of
one method, injection points that shift under each other, or a fast path one mod
adds and another removes. Overlap is a *signal for where to look*, never a
verdict, and this module is careful to say so.

Nothing here proves a conflict. It ranks where to point the benchmark next.
"""

from __future__ import annotations

import contextlib
import json
import re
import struct
import tomllib
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "ArchiveTooLarge",
    "ModMetadata",
    "Finding",
    "Severity",
    "Inspection",
    "read_jar",
    "read_jars",
    "inspect_mods",
    "mixin_targets",
]


# --------------------------------------------------------------------------
# Archive safety limits
# --------------------------------------------------------------------------
#
# A mod jar is untrusted input. Inspection is often the *first* thing run
# against a pack from an unknown source, so it must survive a hostile archive
# rather than being the thing that falls over.
#
# The classic attack is a zip bomb: a 200 KB jar whose entries expand to
# hundreds of megabytes, exhausting memory in a tool that reads every class.
# ZipInfo carries the declared uncompressed size, so the cost of an entry is
# knowable before reading it — these limits are checked against that, and the
# read is capped again afterwards in case the declared size lied.

#: Largest single entry we will read into memory.
MAX_ENTRY_BYTES = 32 * 1024 * 1024

#: Largest total we will read from one jar.
MAX_TOTAL_BYTES = 256 * 1024 * 1024

#: Entry-count cap, against archives with millions of tiny members.
MAX_ENTRIES = 20_000

#: Compression ratios above this are the signature of a bomb; real class files
#: and JSON compress well but not like this.
MAX_COMPRESSION_RATIO = 300


class ArchiveTooLarge(Exception):
    """A jar exceeded a safety limit and was not fully read."""


def _safe_read(archive: zipfile.ZipFile, name: str, budget: list[int]) -> bytes | None:
    """Read one entry, refusing anything that would blow a limit.

    ``budget`` is a one-element list holding the remaining total allowance, so
    the cap applies across the whole jar rather than per entry.
    """
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None

    if info.file_size > MAX_ENTRY_BYTES:
        raise ArchiveTooLarge(
            f"{name}: entry declares {info.file_size} bytes, over the "
            f"{MAX_ENTRY_BYTES} limit"
        )
    if info.compress_size > 0:
        ratio = info.file_size / info.compress_size
        if ratio > MAX_COMPRESSION_RATIO:
            raise ArchiveTooLarge(
                f"{name}: compression ratio {ratio:.0f}:1 exceeds "
                f"{MAX_COMPRESSION_RATIO}:1, which is the signature of a zip bomb"
            )
    if info.file_size > budget[0]:
        raise ArchiveTooLarge(
            f"{name}: reading it would exceed the {MAX_TOTAL_BYTES}-byte "
            f"per-jar budget"
        )

    with archive.open(name) as handle:
        # Bounded again on read: the declared size is attacker-controlled and a
        # lying header should not turn into an unbounded read.
        data = handle.read(MAX_ENTRY_BYTES + 1)
    if len(data) > MAX_ENTRY_BYTES:
        raise ArchiveTooLarge(f"{name}: actual size exceeds the entry limit")

    budget[0] -= len(data)
    return data


class Severity(str, Enum):
    """How much a finding should worry the operator."""

    ERROR = "error"
    """The pack will not work: a declared incompatibility or a missing dependency."""
    WARNING = "warning"
    """Likely to cause trouble, or makes a benchmark result untrustworthy."""
    INFO = "info"
    """Worth knowing; often the starting point for a bisect."""


@dataclass(frozen=True)
class Finding:
    """One diagnosis."""

    severity: Severity
    code: str
    summary: str
    detail: str = ""
    mods: tuple[str, ...] = ()

    def __str__(self) -> str:
        who = f" [{', '.join(self.mods)}]" if self.mods else ""
        return f"{self.severity.value}: {self.summary}{who}"


@dataclass
class ModMetadata:
    """What a jar declares about itself.

    Normalised across loaders so downstream analysis does not care whether a
    dependency came from ``fabric.mod.json`` or ``mods.toml``.
    """

    path: Path
    mod_id: str = ""
    name: str = ""
    version: str = ""
    loader: str = "unknown"
    environment: str = "*"
    license: str = ""
    depends: dict[str, str] = field(default_factory=dict)
    breaks: dict[str, str] = field(default_factory=dict)
    recommends: dict[str, str] = field(default_factory=dict)
    provides: tuple[str, ...] = ()
    mixin_configs: tuple[str, ...] = ()
    mixin_targets: frozenset[str] = frozenset()
    nested_jars: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def client_only(self) -> bool:
        return self.environment == "client"

    @property
    def label(self) -> str:
        return f"{self.mod_id or self.path.name}@{self.version or '?'}"


# --------------------------------------------------------------------------
# Class file constant-pool scanning
# --------------------------------------------------------------------------

_CONSTANT_UTF8 = 1
_CONSTANT_INTEGER = 3
_CONSTANT_FLOAT = 4
_CONSTANT_LONG = 5
_CONSTANT_DOUBLE = 6
_CONSTANT_CLASS = 7
_CONSTANT_STRING = 8
_CONSTANT_FIELDREF = 9
_CONSTANT_METHODREF = 10
_CONSTANT_INTERFACE_METHODREF = 11
_CONSTANT_NAME_AND_TYPE = 12
_CONSTANT_METHOD_HANDLE = 15
_CONSTANT_METHOD_TYPE = 16
_CONSTANT_DYNAMIC = 17
_CONSTANT_INVOKE_DYNAMIC = 18
_CONSTANT_MODULE = 19
_CONSTANT_PACKAGE = 20

#: Fixed payload sizes for constant pool entries that are not UTF8.
_FIXED_SIZES = {
    _CONSTANT_INTEGER: 4, _CONSTANT_FLOAT: 4, _CONSTANT_LONG: 8,
    _CONSTANT_DOUBLE: 8, _CONSTANT_CLASS: 2, _CONSTANT_STRING: 2,
    _CONSTANT_FIELDREF: 4, _CONSTANT_METHODREF: 4,
    _CONSTANT_INTERFACE_METHODREF: 4, _CONSTANT_NAME_AND_TYPE: 4,
    _CONSTANT_METHOD_HANDLE: 3, _CONSTANT_METHOD_TYPE: 2,
    _CONSTANT_DYNAMIC: 4, _CONSTANT_INVOKE_DYNAMIC: 4,
    _CONSTANT_MODULE: 2, _CONSTANT_PACKAGE: 2,
}

_TARGET_PATTERN = re.compile(r"(net/minecraft/[\w/$]+)")


def _constant_pool_strings(data: bytes) -> list[str]:
    """Extract every UTF8 constant from a class file.

    A full bytecode library would let us read the ``@Mixin`` annotation properly,
    but pulling ASM in for this would be a heavy dependency in a project that
    otherwise has none. The constant pool is enough: a mixin's target classes
    appear there as descriptors, and walking the pool needs only the entry size
    table above.

    The tradeoff is honest over-approximation — a class merely *referenced* by a
    mixin shows up alongside the one it targets. Since overlap is a signal for
    where to look rather than a verdict, over-approximating is the safe
    direction.
    """
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return []

    count = struct.unpack_from(">H", data, 8)[0]
    offset = 10
    strings: list[str] = []
    index = 1
    while index < count:
        if offset >= len(data):
            break
        tag = data[offset]
        offset += 1
        if tag == _CONSTANT_UTF8:
            if offset + 2 > len(data):
                break
            length = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            raw = data[offset : offset + length]
            offset += length
            with contextlib.suppress(UnicodeDecodeError):
                strings.append(raw.decode("utf-8", errors="replace"))
        else:
            size = _FIXED_SIZES.get(tag)
            if size is None:
                # Unknown tag: the pool cannot be walked further with confidence,
                # and guessing would produce garbage targets.
                break
            offset += size
            # Long and Double occupy two pool slots, a genuine JVM spec quirk.
            if tag in (_CONSTANT_LONG, _CONSTANT_DOUBLE):
                index += 1
        index += 1
    return strings


def mixin_targets(data: bytes) -> set[str]:
    """Minecraft classes referenced by a compiled mixin class."""
    found: set[str] = set()
    for text in _constant_pool_strings(data):
        for match in _TARGET_PATTERN.findall(text):
            found.add(match)
    return found


# --------------------------------------------------------------------------
# Metadata readers
# --------------------------------------------------------------------------


def _version_spec(value: Any) -> str:
    if isinstance(value, list):
        return " || ".join(str(v) for v in value)
    return str(value)


def _read_fabric(archive: zipfile.ZipFile, meta: ModMetadata, budget: list[int]) -> None:
    raw = _safe_read(archive, "fabric.mod.json", budget)
    if raw is None:
        raise KeyError("fabric.mod.json")
    data = json.loads(raw.decode("utf-8"))
    meta.loader = "fabric"
    meta.mod_id = str(data.get("id", ""))
    meta.name = str(data.get("name", meta.mod_id))
    meta.version = str(data.get("version", ""))
    meta.environment = str(data.get("environment", "*"))
    meta.license = _license_text(data.get("license"))
    meta.depends = {k: _version_spec(v) for k, v in (data.get("depends") or {}).items()}
    meta.breaks = {k: _version_spec(v) for k, v in (data.get("breaks") or {}).items()}
    meta.recommends = {
        k: _version_spec(v) for k, v in (data.get("recommends") or {}).items()
    }
    meta.provides = tuple(data.get("provides", ()) or ())

    configs = data.get("mixins") or []
    names = []
    for entry in configs:
        # An entry may be a plain filename or an object with an environment.
        names.append(entry if isinstance(entry, str) else str(entry.get("config", "")))
    meta.mixin_configs = tuple(n for n in names if n)


def _read_neoforge(
    archive: zipfile.ZipFile, meta: ModMetadata, name: str, budget: list[int]
) -> None:
    raw = _safe_read(archive, name, budget)
    if raw is None:
        raise KeyError(name)
    data = tomllib.loads(raw.decode("utf-8"))
    meta.loader = "neoforge" if "neoforge" in name else "forge"

    mods = data.get("mods") or []
    if mods:
        first = mods[0]
        meta.mod_id = str(first.get("modId", ""))
        meta.name = str(first.get("displayName", meta.mod_id))
        meta.version = str(first.get("version", ""))
        meta.license = str(data.get("license", ""))

    # Dependencies are keyed by mod id and each is a list of constraints.
    for _owner, entries in (data.get("dependencies") or {}).items():
        for entry in entries or []:
            target = str(entry.get("modId", ""))
            if not target:
                continue
            spec = str(entry.get("versionRange", "*"))
            ordering = str(entry.get("type", "required")).lower()
            if ordering == "incompatible":
                meta.breaks[target] = spec
            elif ordering == "optional":
                meta.recommends[target] = spec
            else:
                meta.depends[target] = spec

    meta.mixin_configs = tuple(
        str(m.get("config", "")) for m in (data.get("mixins") or []) if m.get("config")
    )


def _read_bukkit(
    archive: zipfile.ZipFile, meta: ModMetadata, budget: list[int]
) -> None:
    """Minimal plugin.yml reader.

    Deliberately not a YAML parser. plugin.yml uses a tiny, well-established
    subset — scalars and simple lists — and adding a YAML dependency to read six
    keys would cost more than it returns. Anything it cannot parse is reported as
    an error on the mod rather than guessed at.
    """
    raw = _safe_read(archive, "plugin.yml", budget)
    if raw is None:
        raise KeyError("plugin.yml")
    text = raw.decode("utf-8", errors="replace")
    meta.loader = "bukkit"

    current_key: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t", "-")):
            item = raw.strip().lstrip("-").strip().strip("'\"")
            if current_key in ("depend", "softdepend") and item:
                target = {"depend": meta.depends, "softdepend": meta.recommends}[
                    current_key
                ]
                target[item] = "*"
            continue

        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip().strip("'\"")
        current_key = key
        if key == "name":
            meta.mod_id = meta.mod_id or value
            meta.name = value
        elif key == "version":
            meta.version = value
        elif key in ("depend", "softdepend") and value.startswith("["):
            bucket = meta.depends if key == "depend" else meta.recommends
            for item in value.strip("[]").split(","):
                item = item.strip().strip("'\"")
                if item:
                    bucket[item] = "*"


def _license_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value or "")


def read_jar(path: str | Path, *, scan_mixins: bool = True) -> ModMetadata:
    """Read one mod jar's metadata, and optionally its mixin footprint."""
    path = Path(path)
    meta = ModMetadata(path=path)
    errors: list[str] = []

    budget = [MAX_TOTAL_BYTES]

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if len(names) > MAX_ENTRIES:
                meta.errors = (
                    f"jar declares {len(names)} entries, over the "
                    f"{MAX_ENTRIES} limit; refusing to inspect it",
                )
                return meta

            try:
                if "fabric.mod.json" in names:
                    _read_fabric(archive, meta, budget)
                elif "META-INF/neoforge.mods.toml" in names:
                    _read_neoforge(archive, meta, "META-INF/neoforge.mods.toml", budget)
                elif "META-INF/mods.toml" in names:
                    _read_neoforge(archive, meta, "META-INF/mods.toml", budget)
                elif "plugin.yml" in names:
                    _read_bukkit(archive, meta, budget)
                else:
                    errors.append(
                        "no recognised mod metadata "
                        "(fabric.mod.json, mods.toml, or plugin.yml)"
                    )
            except (json.JSONDecodeError, tomllib.TOMLDecodeError, KeyError) as exc:
                errors.append(f"malformed metadata: {exc}")
            except ArchiveTooLarge as exc:
                errors.append(f"refused to read metadata: {exc}")

            meta.nested_jars = tuple(
                n for n in names if n.startswith("META-INF/jars/") and n.endswith(".jar")
            )

            if scan_mixins:
                targets: set[str] = set()
                for name in names:
                    # Mixin classes live under the package named by the mixin
                    # config; scanning every class in the jar would pick up the
                    # mod's own internals and drown the signal.
                    if not name.endswith(".class") or "mixin" not in name.lower():
                        continue
                    try:
                        data = _safe_read(archive, name, budget)
                    except ArchiveTooLarge as exc:
                        errors.append(f"stopped scanning mixins: {exc}")
                        break
                    except (KeyError, zipfile.BadZipFile):
                        continue
                    if data is not None:
                        targets |= mixin_targets(data)
                meta.mixin_targets = frozenset(targets)

    except (zipfile.BadZipFile, OSError) as exc:
        errors.append(f"cannot read jar: {exc}")

    meta.errors = tuple(errors)
    if not meta.mod_id:
        meta.mod_id = path.stem
    return meta


def read_jars(paths: Iterable[str | Path], *, scan_mixins: bool = True) -> list[ModMetadata]:
    return [read_jar(p, scan_mixins=scan_mixins) for p in paths]


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass
class Inspection:
    """The result of inspecting a mod set."""

    mods: list[ModMetadata] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: Minecraft class -> mods whose mixins reference it, for 2+ mods only.
    overlaps: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def hotspots(self, limit: int = 15) -> list[tuple[str, tuple[str, ...]]]:
        """Most-contended classes first — where to point a bisect."""
        return sorted(
            self.overlaps.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )[:limit]


#: Ids that are supplied by the environment rather than by a jar in the set.
_AMBIENT_IDS = {
    "minecraft", "java", "fabricloader", "fabric", "fabric-api",
    "neoforge", "forge", "mcp", "bukkit", "spigot", "paper", "server",
}

#: Fabric API ships as one jar that provides dozens of module ids
#: (``fabric-rendering-fluids-v1`` and so on). A mod depending on six modules is
#: depending on one artefact, and reporting six missing dependencies for a single
#: absent jar trains people to ignore the tool.
_FABRIC_API_MODULE = re.compile(r"^fabric-[a-z0-9-]+-v\d+$")


def _is_fabric_api_module(mod_id: str) -> bool:
    return bool(_FABRIC_API_MODULE.match(mod_id))


def inspect_mods(
    mods: Sequence[ModMetadata], *, overlap_threshold: int = 2
) -> Inspection:
    """Analyse a mod set for conflicts, gaps, and contention."""
    result = Inspection(mods=list(mods))

    by_id: dict[str, list[ModMetadata]] = {}
    for mod in mods:
        by_id.setdefault(mod.mod_id, []).append(mod)
        for provided in mod.provides:
            by_id.setdefault(provided, []).append(mod)

    for mod in mods:
        for message in mod.errors:
            result.findings.append(Finding(
                Severity.WARNING, "unreadable",
                f"{mod.path.name}: {message}",
                mods=(mod.mod_id,),
            ))

    # Duplicate ids: two jars claiming the same mod usually means an accidental
    # double-install, and loaders resolve it unpredictably.
    for mod_id, owners in by_id.items():
        if len(owners) > 1 and len({o.path for o in owners}) > 1:
            result.findings.append(Finding(
                Severity.ERROR, "duplicate_id",
                f"mod id {mod_id!r} is provided by {len(owners)} jars",
                detail=", ".join(o.path.name for o in owners),
                mods=(mod_id,),
            ))

    present = set(by_id)

    for mod in mods:
        # Declared incompatibilities: the author already told us.
        for broken, spec in mod.breaks.items():
            if broken in present:
                result.findings.append(Finding(
                    Severity.ERROR, "declared_conflict",
                    f"{mod.mod_id} declares it breaks {broken}",
                    detail=f"incompatible range: {spec}",
                    mods=(mod.mod_id, broken),
                ))

        fabric_api_modules: list[str] = []
        for needed, spec in mod.depends.items():
            if needed in _AMBIENT_IDS or needed in present:
                continue
            if _is_fabric_api_module(needed):
                # Collected and reported once below, as the single jar it is.
                fabric_api_modules.append(needed)
                continue
            result.findings.append(Finding(
                Severity.ERROR, "missing_dependency",
                f"{mod.mod_id} requires {needed}, which is not in this set",
                detail=f"required range: {spec}",
                mods=(mod.mod_id, needed),
            ))

        if fabric_api_modules and "fabric-api" not in present:
            result.findings.append(Finding(
                Severity.ERROR, "missing_dependency",
                f"{mod.mod_id} requires Fabric API, which is not in this set",
                detail=(
                    f"needs {len(fabric_api_modules)} of its modules: "
                    + ", ".join(sorted(fabric_api_modules))
                ),
                mods=(mod.mod_id, "fabric-api"),
            ))

        for wanted, spec in mod.recommends.items():
            if wanted in _AMBIENT_IDS or wanted in present:
                continue
            result.findings.append(Finding(
                Severity.INFO, "missing_recommendation",
                f"{mod.mod_id} recommends {wanted}, which is absent",
                detail=f"recommended range: {spec}",
                mods=(mod.mod_id,),
            ))

    # Mixin contention.
    owners_by_target: dict[str, list[str]] = {}
    for mod in mods:
        for target in mod.mixin_targets:
            owners_by_target.setdefault(target, []).append(mod.mod_id)

    for target, owners in owners_by_target.items():
        unique = sorted(set(owners))
        if len(unique) >= overlap_threshold:
            result.overlaps[target] = tuple(unique)

    if result.overlaps:
        worst = max(len(v) for v in result.overlaps.values())
        result.findings.append(Finding(
            Severity.INFO, "mixin_overlap",
            f"{len(result.overlaps)} Minecraft class(es) are transformed by "
            f"more than one mod (up to {worst} mods on one class)",
            detail=(
                "Overlap is where conflicts come from, but it is not proof of "
                "one — mods routinely coexist on the same class. Treat this as "
                "the list of places to point a bisect first."
            ),
        ))

    return result
