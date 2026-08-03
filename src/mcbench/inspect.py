"""Static inspection of a mod set: metadata, conflicts, and mixin overlap.

Everything here works on jar files alone: no game, no account, no GPU, no run.
That matters for a modpack health check, because the fastest useful answer is the
one you get before spending two hours benchmarking a pack that was never going to
load.

Three classes of finding:

**Declared incompatibilities.** Mod authors already record what they break. A
Fabric mod's ``breaks`` block, a NeoForge ``incompatible`` dependency, a Bukkit
``depend`` list, all of it machine-readable and routinely ignored. Reading it
catches the conflicts someone already knew about.

**Structural problems.** Missing dependencies, duplicate mod ids, and two jars
providing the same id are pack-assembly mistakes that produce confusing runtime
failures.

**Mixin target overlap.** The interesting one, and the reason this module parses
bytecode. Two mods that transform the same Minecraft class are not necessarily
broken, but that is where conflicts actually come from: competing rewrites of
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

from .versions import Dialect as VersionDialect
from .versions import Satisfaction, satisfies

__all__ = [
    "ArchiveTooLarge",
    "ModMetadata",
    "MixinConfig",
    "Finding",
    "Severity",
    "Inspection",
    "InspectionTarget",
    "read_jar",
    "read_jars",
    "inspect_mods",
    "mixin_targets",
    "is_ambient",
    "flatten_mods",
    "AMBIENT_IDS",
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
# knowable before reading it. These limits are checked against that, and the
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

#: How deep nested jars are followed. Fabric mods bundle their dependencies in
#: ``META-INF/jars/``, and those jars may bundle their own, but only to a
#: shallow depth in practice, while a hostile archive can nest indefinitely and
#: turn inspection into an unbounded recursion over a few kilobytes.
MAX_NESTING_DEPTH = 3

#: Nested jars followed per archive. A legitimate library bundle holds a handful;
#: a thousand is an attack on the inspector's time, not a modpack.
MAX_NESTED_JARS = 64


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
class MixinConfig:
    """One declared mixin configuration, as resolved from the jar.

    Selecting classes by a "mixin" substring in the path misses every mixin in
    a differently named package and picks up every helper beside one; both
    errors land in the contention ranking a bisect is pointed at.
    """

    resource: str
    package: str = ""
    common: tuple[str, ...] = ()
    client: tuple[str, ...] = ()
    server: tuple[str, ...] = ()
    plugin: str = ""
    """A mixin plugin can add or suppress mixins at runtime, so its presence
    makes the declared list a lower bound."""
    error: str = ""

    @property
    def classes(self) -> tuple[str, ...]:
        """Every declared mixin class, as an internal (slash-separated) name."""
        prefix = self.package.replace(".", "/")
        names = (*self.common, *self.client, *self.server)
        return tuple(
            f"{prefix}/{name.replace('.', '/')}" if prefix else name.replace(".", "/")
            for name in names
        )


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
    configs: tuple[MixinConfig, ...] = ()
    """The declared configurations, parsed. Empty when none were declared."""
    mixin_targets: frozenset[str] = frozenset()
    """Classes a mixin *references*. An over-approximation by construction: a
    class merely used by a mixin appears alongside the one it transforms."""
    verified_targets: frozenset[str] = frozenset()
    """Classes an ``@Mixin`` annotation names as a target: a subset of
    :attr:`mixin_targets`, and the only one that is evidence rather than a
    hint."""
    nested_jars: tuple[str, ...] = ()
    nested: tuple[ModMetadata, ...] = ()
    """Metadata from bundled jars. Fabric libraries ship this way, and treating
    one as absent reports a missing dependency for a complete pack."""
    unreadable: bool = False
    """The archive or its descriptor could not be read, so nothing this jar
    declares is known."""
    errors: tuple[str, ...] = ()

    @property
    def client_only(self) -> bool:
        return self.environment == "client"

    @property
    def label(self) -> str:
        return f"{self.mod_id or self.path.name}@{self.version or '?'}"

    @property
    def uses_mixin_plugin(self) -> bool:
        return any(config.plugin for config in self.configs)

    def provided_ids(self) -> dict[str, str]:
        """Ids this jar itself supplies, mapped to the version it supplies them at.

        Bundled jars are not folded in: :func:`flatten_mods` surfaces them
        separately, and counting them twice looks like a double-install.
        """
        supplied = {self.mod_id: self.version} if self.mod_id else {}
        for alias in self.provides:
            supplied.setdefault(alias, self.version)
        return supplied


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

    The tradeoff is honest over-approximation: a class merely referenced by a
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
    """Minecraft classes referenced by a compiled mixin class.

    An over-approximation by design; see :func:`_constant_pool_strings`. Use
    :func:`annotated_mixin_targets` for declared targets.
    """
    found: set[str] = set()
    for text in _constant_pool_strings(data):
        for match in _TARGET_PATTERN.findall(text):
            found.add(match)
    return found


def annotated_mixin_targets(data: bytes) -> set[str] | None:
    """Targets named by the class's ``@Mixin`` annotation.

    None when the class carries no ``@Mixin`` annotation, which is how a helper
    in a mixin package is told apart from a mixin.

    Both forms are read: ``@Mixin(Foo.class)`` stores class constants, and
    ``@Mixin(targets = "a.b.Foo")`` stores dotted names as strings, and the latter
    for inner and package-private classes, which are disproportionately the
    interesting targets.

    Walks the attribute table rather than pulling in a bytecode library, which
    keeps the project dependency-free.
    """
    parsed = _parse_class(data)
    if parsed is None:
        return None
    pool, offset = parsed

    try:
        # access_flags, this_class, super_class
        offset += 6
        interfaces = struct.unpack_from(">H", data, offset)[0]
        offset += 2 + interfaces * 2
        offset = _skip_members(data, offset, pool)  # fields
        offset = _skip_members(data, offset, pool)  # methods
        attribute_count = struct.unpack_from(">H", data, offset)[0]
        offset += 2

        for _ in range(attribute_count):
            name_index, length = struct.unpack_from(">HI", data, offset)
            offset += 6
            name = pool.get(name_index)
            if name in ("RuntimeVisibleAnnotations", "RuntimeInvisibleAnnotations"):
                targets = _mixin_annotation_targets(data, offset, pool)
                if targets is not None:
                    return targets
            offset += length
    except (struct.error, IndexError, KeyError):
        # Truncated or crafted. None loses information; claiming targets read
        # out of malformed bytes would invent it.
        return None
    return None


def _parse_class(data: bytes) -> tuple[dict[int, Any], int] | None:
    """Read the constant pool, returning ``(pool, offset_after_pool)``."""
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return None

    count = struct.unpack_from(">H", data, 8)[0]
    offset = 10
    pool: dict[int, Any] = {}
    index = 1
    while index < count:
        if offset >= len(data):
            return None
        tag = data[offset]
        offset += 1
        if tag == _CONSTANT_UTF8:
            if offset + 2 > len(data):
                return None
            length = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            pool[index] = data[offset : offset + length].decode(
                "utf-8", errors="replace"
            )
            offset += length
        else:
            size = _FIXED_SIZES.get(tag)
            if size is None:
                return None
            if tag == _CONSTANT_CLASS:
                # Resolved lazily, so the pool walks in one pass despite
                # forward references.
                pool[index] = _ClassRef(struct.unpack_from(">H", data, offset)[0])
            offset += size
            if tag in (_CONSTANT_LONG, _CONSTANT_DOUBLE):
                index += 1
        index += 1
    return pool, offset


@dataclass(frozen=True)
class _ClassRef:
    name_index: int


def _resolve_class(pool: dict[int, Any], index: int) -> str | None:
    entry = pool.get(index)
    if isinstance(entry, _ClassRef):
        name = pool.get(entry.name_index)
        return name if isinstance(name, str) else None
    return entry if isinstance(entry, str) else None


def _skip_members(data: bytes, offset: int, pool: dict[int, Any]) -> int:
    """Skip a field_info or method_info table."""
    count = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    for _ in range(count):
        offset += 6  # access_flags, name_index, descriptor_index
        attributes = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        for _ in range(attributes):
            length = struct.unpack_from(">I", data, offset + 2)[0]
            offset += 6 + length
    return offset


_MIXIN_DESCRIPTORS = (
    "Lorg/spongepowered/asm/mixin/Mixin;",
    "Lorg/spongepowered/asm/mixin/Pseudo;",
)


def _mixin_annotation_targets(
    data: bytes, offset: int, pool: dict[int, Any]
) -> set[str] | None:
    count = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    for _ in range(count):
        type_index = struct.unpack_from(">H", data, offset)[0]
        descriptor = pool.get(type_index)
        targets: set[str] = set()
        is_mixin = descriptor in _MIXIN_DESCRIPTORS
        offset = _read_annotation_values(data, offset + 2, pool, targets, is_mixin)
        if is_mixin:
            return targets
    return None


def _read_annotation_values(
    data: bytes,
    offset: int,
    pool: dict[int, Any],
    targets: set[str],
    collect: bool,
) -> int:
    pairs = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    for _ in range(pairs):
        name_index = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        name = pool.get(name_index)
        offset = _read_element_value(
            data, offset, pool, targets,
            collect and name in ("value", "targets"),
        )
    return offset


def _read_element_value(
    data: bytes,
    offset: int,
    pool: dict[int, Any],
    targets: set[str],
    collect: bool,
) -> int:
    tag = data[offset]
    offset += 1
    if tag == ord("c"):  # class constant: @Mixin(Foo.class)
        index = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        descriptor = pool.get(index)
        if collect and isinstance(descriptor, str):
            name = descriptor.strip("L;").lstrip("[")
            if name:
                targets.add(name)
    elif tag == ord("s"):  # string: @Mixin(targets = "net.minecraft.Foo")
        index = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        value = pool.get(index)
        if collect and isinstance(value, str) and value:
            targets.add(value.replace(".", "/"))
    elif tag == ord("e"):  # enum: type_name_index, const_name_index
        offset += 4
    elif tag == ord("@"):  # nested annotation
        offset += 2
        offset = _read_annotation_values(data, offset, pool, targets, False)
    elif tag == ord("["):
        length = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        for _ in range(length):
            offset = _read_element_value(data, offset, pool, targets, collect)
    else:  # primitive or Utf8 constant, always a 2-byte pool index
        offset += 2
    return offset


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
    subset, scalars and simple lists, and adding a YAML dependency to read six
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


def _read_mixin_config(
    archive: zipfile.ZipFile, resource: str, budget: list[int]
) -> MixinConfig:
    """Parse one declared mixin configuration JSON."""
    config = MixinConfig(resource=resource)
    try:
        raw = _safe_read(archive, resource, budget)
    except ArchiveTooLarge as exc:
        config.error = str(exc)
        return config
    if raw is None:
        # Declared and absent: a config the loader cannot find is a hard
        # startup failure.
        config.error = "declared in the mod metadata but not present in the jar"
        return config

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        config.error = f"malformed mixin config: {exc}"
        return config
    if not isinstance(data, dict):
        config.error = "mixin config is not an object"
        return config

    def names(key: str) -> tuple[str, ...]:
        value = data.get(key) or []
        if not isinstance(value, list):
            return ()
        return tuple(str(v) for v in value if isinstance(v, str))

    config.package = str(data.get("package", ""))
    config.common = names("mixins")
    config.client = names("client")
    config.server = names("server")
    config.plugin = str(data.get("plugin", "") or "")
    return config


def _scan_mixin_classes(
    archive: zipfile.ZipFile,
    meta: ModMetadata,
    names: set[str],
    budget: list[int],
    errors: list[str],
) -> None:
    """Resolve declared mixin classes, and read their real targets.

    Declared classes are the authority. Where a jar declares nothing, the
    path-substring heuristic is the fallback and its results stay in
    :attr:`ModMetadata.mixin_targets`, never promoted to verified.
    """
    declared: list[str] = []
    for config in meta.configs:
        if config.error:
            errors.append(f"{config.resource}: {config.error}")
        declared.extend(f"{name}.class" for name in config.classes)

    if declared:
        candidates = [name for name in declared if name in names]
        undeclared = [name for name in declared if name not in names]
        if undeclared:
            errors.append(
                f"{len(undeclared)} declared mixin class(es) are missing from the "
                f"jar: {', '.join(sorted(undeclared)[:5])}"
            )
    else:
        # Nothing declared: fall back to the heuristic, over-approximate in
        # both directions and labelled as such.
        candidates = [
            name for name in names
            if name.endswith(".class") and "mixin" in name.lower()
        ]

    referenced: set[str] = set()
    verified: set[str] = set()
    for name in sorted(candidates):
        try:
            data = _safe_read(archive, name, budget)
        except ArchiveTooLarge as exc:
            errors.append(f"stopped scanning mixins: {exc}")
            break
        except (KeyError, zipfile.BadZipFile):
            continue
        if data is None:
            continue
        referenced |= mixin_targets(data)
        annotated = annotated_mixin_targets(data)
        if annotated is not None:
            verified |= {t for t in annotated if t.startswith("net/minecraft/")}

    meta.mixin_targets = frozenset(referenced)
    meta.verified_targets = frozenset(verified)


def read_jar(
    path: str | Path, *, scan_mixins: bool = True, _depth: int = 0
) -> ModMetadata:
    """Read one mod jar's metadata, its mixin configurations, and its bundles.

    Nested jars are followed to :data:`MAX_NESTING_DEPTH`: Fabric libraries ship
    inside their dependents, and treating those as absent reports a missing
    dependency for a complete pack.
    """
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
                meta.unreadable = True
                if not meta.mod_id:
                    meta.mod_id = path.stem
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
                    meta.unreadable = True
            except (json.JSONDecodeError, tomllib.TOMLDecodeError, KeyError) as exc:
                errors.append(f"malformed metadata: {exc}")
                meta.unreadable = True
            except ArchiveTooLarge as exc:
                errors.append(f"refused to read metadata: {exc}")
                meta.unreadable = True

            meta.configs = tuple(
                _read_mixin_config(archive, resource, budget)
                for resource in meta.mixin_configs
            )

            meta.nested_jars = tuple(sorted(
                n for n in names
                if n.startswith("META-INF/jars/") and n.endswith(".jar")
            ))
            if meta.nested_jars and _depth < MAX_NESTING_DEPTH:
                meta.nested = _read_nested(
                    archive, meta, budget, errors,
                    scan_mixins=scan_mixins, depth=_depth,
                )
            elif meta.nested_jars:
                errors.append(
                    f"{len(meta.nested_jars)} nested jar(s) were not inspected: "
                    f"nesting deeper than {MAX_NESTING_DEPTH} levels"
                )

            if scan_mixins:
                _scan_mixin_classes(archive, meta, names, budget, errors)

    except (zipfile.BadZipFile, OSError) as exc:
        errors.append(f"cannot read jar: {exc}")
        meta.unreadable = True

    meta.errors = tuple(errors)
    if not meta.mod_id:
        meta.mod_id = path.stem
    return meta


def _read_nested(
    archive: zipfile.ZipFile,
    meta: ModMetadata,
    budget: list[int],
    errors: list[str],
    *,
    scan_mixins: bool,
    depth: int,
) -> tuple[ModMetadata, ...]:
    """Inspect bundled jars by extracting each to a temporary file.

    Extracted because ``zipfile`` needs a seekable source, and holding every
    nested archive in memory would put an attacker-chosen size on the heap. The
    shared ``budget`` still applies.
    """
    import tempfile

    found: list[ModMetadata] = []
    if len(meta.nested_jars) > MAX_NESTED_JARS:
        errors.append(
            f"jar bundles {len(meta.nested_jars)} nested jars, over the "
            f"{MAX_NESTED_JARS} limit; none were inspected"
        )
        return ()

    with tempfile.TemporaryDirectory(prefix="mcbench-nested-") as workspace:
        for resource in meta.nested_jars:
            try:
                data = _safe_read(archive, resource, budget)
            except ArchiveTooLarge as exc:
                errors.append(f"{resource}: {exc}")
                break
            except (KeyError, zipfile.BadZipFile) as exc:
                errors.append(f"{resource}: {exc}")
                continue
            if data is None:
                continue

            # Flattened: a nested entry path is attacker-controlled.
            target = Path(workspace) / f"{len(found)}-{Path(resource).name}"
            target.write_bytes(data)
            nested = read_jar(target, scan_mixins=scan_mixins, _depth=depth + 1)
            # The temporary path is meaningless to a reader; name the entry.
            nested.path = meta.path / resource
            found.append(nested)

    return tuple(found)


def read_jars(paths: Iterable[str | Path], *, scan_mixins: bool = True) -> list[ModMetadata]:
    return [read_jar(p, scan_mixins=scan_mixins) for p in paths]


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectionTarget:
    """What the pack is being inspected *against*.

    Ranges cannot be evaluated without one: ``depends = {"minecraft": ">=1.21"}``
    is the commonest declaration in the ecosystem and cannot be judged from the
    jars alone. The dialect follows the loader: ``[1.0,2.0)`` and
    ``>=1.0 <2.0`` are the same constraint in two syntaxes.
    """

    loader: str = ""
    minecraft_version: str = ""
    loader_version: str = ""
    #: Ids a provisioning layer guarantees at a compatible version, beyond the
    #: jars supplied. Empty by default.
    provisioned: frozenset[str] = frozenset()

    @property
    def dialect(self) -> VersionDialect:
        return VersionDialect.for_loader(self.loader)

    def environment_versions(self) -> dict[str, str]:
        """Versions for the ids the environment supplies, where known."""
        supplied: dict[str, str] = {}
        if self.minecraft_version:
            supplied["minecraft"] = self.minecraft_version
        if self.loader_version and self.loader:
            supplied[self.loader] = self.loader_version
            if self.loader == "fabric":
                supplied["fabricloader"] = self.loader_version
        return supplied


@dataclass
class Inspection:
    """The result of inspecting a mod set."""

    mods: list[ModMetadata] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: Minecraft class -> mods whose mixins reference it, for 2+ mods only.
    overlaps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Minecraft class -> mods whose ``@Mixin`` annotation names it.
    verified_overlaps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    target: InspectionTarget = field(default_factory=InspectionTarget)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def hotspots(
        self, limit: int = 15, *, verified_only: bool = False
    ) -> list[tuple[str, tuple[str, ...]]]:
        """Most-contended classes first: where to point a bisect."""
        source = self.verified_overlaps if verified_only else self.overlaps
        return sorted(source.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]


#: Ids supplied by the environment rather than by a jar in the set.
#:
#: Fabric API is deliberately **not** here: it is an ordinary mod, absent from a
#: fresh instance, and treating it as ambient hides the dependency most Fabric
#: packs actually miss. Declare it through
#: :attr:`InspectionTarget.provisioned` when a layer guarantees it.
AMBIENT_IDS = frozenset({
    "minecraft", "java", "fabricloader", "quilt_loader", "fabric-loader",
    "neoforge", "forge", "mcp", "bukkit", "spigot", "paper", "server",
})


def is_ambient(mod_id: str) -> bool:
    """Whether an id is supplied by the platform rather than by a jar."""
    return mod_id in AMBIENT_IDS


#: Fabric API ships as one jar providing dozens of module ids
#: (``fabric-rendering-fluids-v1`` and so on), so they collapse into one finding.
_FABRIC_API_MODULE = re.compile(r"^fabric-[a-z0-9-]+-v\d+$")


def _is_fabric_api_module(mod_id: str) -> bool:
    return bool(_FABRIC_API_MODULE.match(mod_id))


def flatten_mods(mods: Iterable[ModMetadata]) -> list[ModMetadata]:
    """Every mod that will be present, including ones bundled inside others.

    A nested jar is installed exactly as if it had been dropped in alongside its
    host, so anything reasoning about what is present has to see it. Inspection
    for dependency satisfaction, and the bisect's dependency graph alike.
    """
    out: list[ModMetadata] = []
    for mod in mods:
        out.append(mod)
        out.extend(flatten_mods(mod.nested))
    return out


def inspect_mods(
    mods: Sequence[ModMetadata],
    *,
    overlap_threshold: int = 2,
    target: InspectionTarget | None = None,
) -> Inspection:
    """Analyse a mod set for conflicts, gaps, and contention.

    Constraints are evaluated by *version*, not presence: checking the id alone
    certifies packs that cannot launch (``lib >=2`` with ``lib 1``) and rejects
    packs that are fine (``breaks lib <2`` against ``lib 3``). An undecidable
    range is reported as a warning, never a silent pass.
    """
    target = target or InspectionTarget()
    result = Inspection(mods=list(mods), target=target)
    dialect = target.dialect

    # Bundled jars are present as if installed alongside.
    everything = flatten_mods(mods)

    versions: dict[str, str] = dict(target.environment_versions())
    by_id: dict[str, list[ModMetadata]] = {}
    for mod in everything:
        for supplied, version in mod.provided_ids().items():
            by_id.setdefault(supplied, []).append(mod)
            versions.setdefault(supplied, version)

    for mod in everything:
        where = mod.path.name
        for message in mod.errors:
            # An error, not a warning: a gate that exits zero on input it could
            # not read is not a gate.
            result.findings.append(Finding(
                Severity.ERROR if mod.unreadable else Severity.WARNING,
                "unreadable",
                f"{where}: {message}",
                mods=(mod.mod_id,),
            ))

    # Duplicate ids: two jars claiming the same mod usually means an accidental
    # double-install, and loaders resolve it unpredictably.
    for mod_id, owners in by_id.items():
        if len(owners) > 1 and len({o.path for o in owners}) > 1:
            result.findings.append(Finding(
                Severity.ERROR, "duplicate_id",
                f"mod id {mod_id!r} is provided by {len(owners)} jars",
                detail=", ".join(str(o.path.name) for o in owners),
                mods=(mod_id,),
            ))

    present = set(by_id)

    for mod in everything:
        _check_conflicts(result, mod, present, versions, dialect)
        _check_dependencies(result, mod, present, versions, dialect, target)

    _check_mixin_overlap(result, everything, overlap_threshold)
    return result


def _check_conflicts(
    result: Inspection,
    mod: ModMetadata,
    present: set[str],
    versions: dict[str, str],
    dialect: VersionDialect,
) -> None:
    """Declared incompatibilities: the author already told us."""
    for broken, spec in mod.breaks.items():
        if broken not in present:
            continue
        installed = versions.get(broken, "")
        verdict = satisfies(installed, spec, dialect=dialect)
        if verdict is Satisfaction.VIOLATED:
            # Outside the forbidden range, so not a conflict.
            continue
        if verdict is Satisfaction.UNKNOWN:
            result.findings.append(Finding(
                Severity.WARNING, "undecidable_conflict",
                f"{mod.mod_id} declares it breaks {broken} {spec}, and whether "
                f"the installed {broken} {installed or '(version unknown)'} "
                f"falls in that range could not be determined",
                detail=(
                    "Neither assumed: calling it a conflict rejects a pack that "
                    "may be fine, calling it clear certifies one that may not "
                    "start."
                ),
                mods=(mod.mod_id, broken),
            ))
            continue
        result.findings.append(Finding(
            Severity.ERROR, "declared_conflict",
            f"{mod.mod_id} declares it breaks {broken} {installed}".rstrip(),
            detail=f"incompatible range: {spec}",
            mods=(mod.mod_id, broken),
        ))


def _check_dependencies(
    result: Inspection,
    mod: ModMetadata,
    present: set[str],
    versions: dict[str, str],
    dialect: VersionDialect,
    target: InspectionTarget,
) -> None:
    fabric_api_modules: list[str] = []
    fabric_api_present = (
        "fabric-api" in present or "fabric-api" in target.provisioned
    )

    for needed, spec in mod.depends.items():
        if _is_fabric_api_module(needed):
            # One jar provides all of these module ids, so six findings for one
            # absent artefact would train people to ignore the tool.
            if not fabric_api_present:
                fabric_api_modules.append(needed)
            continue

        available = (
            needed in present
            or needed in target.provisioned
            or (is_ambient(needed) and needed in versions)
        )
        if not available:
            if is_ambient(needed):
                # Supplied by the platform at a version we were not told, so
                # the range cannot be checked.
                result.findings.append(Finding(
                    Severity.WARNING, "unchecked_dependency",
                    f"{mod.mod_id} requires {needed} {spec}, which the platform "
                    f"supplies at a version this inspection was not given",
                    detail=(
                        "Pass --minecraft-version and --loader-version to check "
                        "it. Until then the constraint is recorded, not verified."
                    ),
                    mods=(mod.mod_id, needed),
                ))
                continue
            result.findings.append(Finding(
                Severity.ERROR, "missing_dependency",
                f"{mod.mod_id} requires {needed}, which is not in this set",
                detail=f"required range: {spec}",
                mods=(mod.mod_id, needed),
            ))
            continue

        installed = versions.get(needed, "")
        verdict = satisfies(installed, spec, dialect=dialect)
        if verdict is Satisfaction.SATISFIED:
            continue
        if verdict is Satisfaction.VIOLATED:
            result.findings.append(Finding(
                Severity.ERROR, "version_conflict",
                f"{mod.mod_id} requires {needed} {spec}, but {needed} "
                f"{installed} is installed",
                detail=(
                    "The id is present and the version is not compatible; the "
                    "loader refuses this at startup."
                ),
                mods=(mod.mod_id, needed),
            ))
            continue
        result.findings.append(Finding(
            Severity.WARNING, "undecidable_dependency",
            f"{mod.mod_id} requires {needed} {spec}, and whether {needed} "
            f"{installed or '(version unknown)'} satisfies it could not be "
            f"determined",
            mods=(mod.mod_id, needed),
        ))

    if fabric_api_modules:
        result.findings.append(Finding(
            Severity.ERROR, "missing_dependency",
            f"{mod.mod_id} requires Fabric API, which is not in this set",
            detail=(
                f"needs {len(fabric_api_modules)} of its modules: "
                + ", ".join(sorted(fabric_api_modules))
                + ". Fabric API is an ordinary mod, not supplied by the loader."
            ),
            mods=(mod.mod_id, "fabric-api"),
        ))

    for wanted, spec in mod.recommends.items():
        if wanted in present or wanted in target.provisioned or is_ambient(wanted):
            continue
        result.findings.append(Finding(
            Severity.INFO, "missing_recommendation",
            f"{mod.mod_id} recommends {wanted}, which is absent",
            detail=f"recommended range: {spec}",
            mods=(mod.mod_id,),
        ))


def _check_mixin_overlap(
    result: Inspection, mods: Sequence[ModMetadata], overlap_threshold: int
) -> None:
    referenced: dict[str, list[str]] = {}
    verified: dict[str, list[str]] = {}
    for mod in mods:
        for target_class in mod.mixin_targets:
            referenced.setdefault(target_class, []).append(mod.mod_id)
        for target_class in mod.verified_targets:
            verified.setdefault(target_class, []).append(mod.mod_id)

    for source, sink in ((referenced, result.overlaps), (verified,
                                                         result.verified_overlaps)):
        for target_class, owners in source.items():
            unique = sorted(set(owners))
            if len(unique) >= overlap_threshold:
                sink[target_class] = tuple(unique)

    if result.verified_overlaps:
        worst = max(len(v) for v in result.verified_overlaps.values())
        result.findings.append(Finding(
            Severity.INFO, "mixin_overlap",
            f"{len(result.verified_overlaps)} Minecraft class(es) are targeted "
            f"by more than one mod's @Mixin annotation (up to {worst} on one "
            f"class)",
            detail=(
                "Declared targets read from the bytecode, not classes that merely "
                "appear near a mixin. Not proof of a conflict, since mods "
                "coexist on the same class routinely, but where to look first."
            ),
        ))
    elif result.overlaps:
        worst = max(len(v) for v in result.overlaps.values())
        result.findings.append(Finding(
            Severity.INFO, "mixin_footprint_overlap",
            f"{len(result.overlaps)} Minecraft class(es) are referenced by more "
            f"than one mod's mixin code (up to {worst} mods on one class)",
            detail=(
                "No @Mixin targets could be read, so this is the constant-pool "
                "footprint: classes a mixin mentions, including ones it only "
                "calls into. Weaker evidence than a declared target."
            ),
        ))

    plugins = [m.mod_id for m in mods if m.uses_mixin_plugin]
    if plugins:
        result.findings.append(Finding(
            Severity.INFO, "mixin_plugin",
            f"{len(plugins)} mod(s) use a mixin plugin, which can add or "
            f"suppress mixins at runtime",
            detail=(
                f"The lists read here are a lower bound for "
                f"{', '.join(sorted(plugins))}; what applies is decided at startup."
            ),
            mods=tuple(sorted(plugins)),
        ))
