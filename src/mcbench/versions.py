"""Version comparison and range evaluation, per loader dialect.

Two dialects, because the ecosystem has two:

* **Fabric** — npm-style semver ranges: ``*``, ``1.2.3``, ``>=1.2``, ``~1.2.3``,
  ``^1.2.3``, ``1.2.x``, space-separated conjunctions, ``||`` alternatives.
* **Forge/NeoForge** — Maven ranges: ``[1.0,2.0)``, ``[1.0,]``, and a bare
  ``1.0`` meaning "1.0 or later" rather than "exactly 1.0". Reading that as
  equality inverts most dependency declarations in the ecosystem.

Comparison is looser than either specification. Real mod versions are not clean
semver — ``mc1.20.1-2.3``, ``0.90.0+1.20.4``, ``4.0.0+1.21`` — and a strict
parser would decline to answer for a large fraction of real packs. Versions are
compared segment by segment, numerics numerically and the rest lexically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Dialect",
    "Version",
    "Satisfaction",
    "parse_version",
    "compare_versions",
    "satisfies",
]


class Dialect(str, Enum):
    """Which range syntax a declaration is written in."""

    FABRIC = "fabric"
    MAVEN = "maven"
    """Forge and NeoForge."""
    BUKKIT = "bukkit"
    """plugin.yml declares dependencies by name only, with no range at all."""

    @classmethod
    def for_loader(cls, loader: str) -> Dialect:
        if loader in ("forge", "neoforge"):
            return cls.MAVEN
        if loader in ("bukkit", "spigot", "paper"):
            return cls.BUKKIT
        return cls.FABRIC


class Satisfaction(str, Enum):
    """Whether a declared constraint holds.

    ``UNKNOWN`` is why this is not a boolean: an unparseable range must not
    become "satisfied" (the presence-only behaviour this replaced) nor
    "violated" (which would flag packs that are fine). Callers report it.
    """

    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


_SEGMENT = re.compile(r"(\d+|[A-Za-z]+)")

#: A leading ``v``, as in ``v1.4``.
_PREFIX = re.compile(r"^(?:v|version)[-_. ]?(?=\d)", re.IGNORECASE)

#: ``mc1.21-0.6.0`` is Sodium 0.6.0 for Minecraft 1.21, not version 1.21 with a
#: pre-release. Stripped only when the ``mc`` marker is present: bare
#: ``1.20.1-2.3`` is ambiguous with a pre-release.
_GAME_TAG = re.compile(r"^mc\d[\w.]*-(?=\d)", re.IGNORECASE)


@dataclass(frozen=True, order=False)
class Version:
    """A comparable version, kept alongside the string it came from."""

    raw: str
    release: tuple[int | str, ...]
    prerelease: tuple[int | str, ...] = ()

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        return self.raw


def parse_version(raw: str) -> Version | None:
    """Parse a version string, or None when nothing in it is comparable.

    Build metadata after ``+`` is discarded per both specifications. Fabric mods
    encode the game version there (``0.92.0+1.20.1``), so comparing it would
    order mods by target version rather than by release.
    """
    text = str(raw).strip()
    if not text:
        return None
    text = _GAME_TAG.sub("", text)
    text = _PREFIX.sub("", text)

    core, _, _build = text.partition("+")
    release_text, _, prerelease_text = core.partition("-")

    release = _segments(release_text)
    if not release or not isinstance(release[0], int):
        # "SNAPSHOT", a git hash: nothing numeric to anchor on, so not
        # comparable. Ordering these would order by spelling.
        return None
    return Version(
        raw=str(raw).strip(),
        release=release,
        prerelease=_segments(prerelease_text),
    )


def _segments(text: str) -> tuple[int | str, ...]:
    out: list[int | str] = []
    for token in _SEGMENT.findall(text or ""):
        out.append(int(token) if token.isdigit() else token.lower())
    return tuple(out)


def _compare_segments(
    left: tuple[int | str, ...], right: tuple[int | str, ...]
) -> int:
    for a, b in zip(left, right, strict=False):
        if a == b:
            continue
        if isinstance(a, int) and isinstance(b, int):
            return -1 if a < b else 1
        # Numeric sorts below alphabetic, per semver's pre-release rule.
        if isinstance(a, int):
            return -1
        if isinstance(b, int):
            return 1
        return -1 if a < b else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def compare_versions(left: Version, right: Version) -> int:
    """Three-way comparison. Negative when ``left`` is older."""
    order = _compare_segments(left.release, right.release)
    if order != 0:
        return order
    # A pre-release is older than the release it leads to: 1.0-rc1 < 1.0.
    if left.is_prerelease and not right.is_prerelease:
        return -1
    if right.is_prerelease and not left.is_prerelease:
        return 1
    return _compare_segments(left.prerelease, right.prerelease)


# --------------------------------------------------------------------------
# Fabric ranges
# --------------------------------------------------------------------------

_FABRIC_OP = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(.+)$")


def _fabric_clause(clause: str, version: Version) -> Satisfaction:
    clause = clause.strip()
    if not clause or clause in ("*", "any"):
        return Satisfaction.SATISFIED

    match = _FABRIC_OP.match(clause)
    if match is None:
        return Satisfaction.UNKNOWN
    operator, operand = match.group(1) or "=", match.group(2).strip()

    # An x-range: 1.2.x, 1.x. Equivalent to a prefix match on the release parts
    # that were actually given.
    if re.search(r"[xX*]", operand):
        prefix = operand.split(".")
        fixed = [p for p in prefix if p not in ("x", "X", "*")]
        wanted = parse_version(".".join(fixed)) if fixed else None
        if wanted is None:
            return Satisfaction.SATISFIED if not fixed else Satisfaction.UNKNOWN
        head = version.release[: len(wanted.release)]
        return (
            Satisfaction.SATISFIED
            if _compare_segments(head, wanted.release) == 0
            else Satisfaction.VIOLATED
        )

    wanted = parse_version(operand)
    if wanted is None:
        return Satisfaction.UNKNOWN

    order = compare_versions(version, wanted)
    if operator == "=":
        # Fabric reads a bare version as a prefix match: "1.2" accepts 1.2.4.
        head = version.release[: len(wanted.release)]
        return (
            Satisfaction.SATISFIED
            if _compare_segments(head, wanted.release) == 0
            else Satisfaction.VIOLATED
        )
    if operator == ">=":
        return _yes(order >= 0)
    if operator == ">":
        return _yes(order > 0)
    if operator == "<=":
        return _yes(order <= 0)
    if operator == "<":
        return _yes(order < 0)
    if operator == "~":
        # Patch-level changes: >=1.2.3 <1.3.0
        return _yes(order >= 0 and _below_bump(version, wanted, index=1))
    if operator == "^":
        # Compatible-with: >=1.2.3 <2.0.0, and for 0.x the leading zero is the
        # compatibility boundary rather than the major.
        index = 0 if wanted.release and wanted.release[0] != 0 else 1
        return _yes(order >= 0 and _below_bump(version, wanted, index=index))
    return Satisfaction.UNKNOWN


def _yes(condition: bool) -> Satisfaction:
    return Satisfaction.SATISFIED if condition else Satisfaction.VIOLATED


def _below_bump(version: Version, wanted: Version, *, index: int) -> bool:
    """True when ``version`` has not incremented the segment at ``index``."""
    if len(wanted.release) <= index:
        return True
    ceiling = list(wanted.release[: index + 1])
    if not isinstance(ceiling[index], int):
        return True
    ceiling[index] = ceiling[index] + 1
    return _compare_segments(version.release[: index + 1], tuple(ceiling)) < 0


def _fabric_range(spec: str, version: Version) -> Satisfaction:
    # Alternatives first: any satisfied alternative satisfies the whole range.
    alternatives = [part for part in spec.split("||")]
    results = []
    for alternative in alternatives:
        clauses = [c for c in alternative.split() if c]
        if not clauses:
            results.append(Satisfaction.SATISFIED)
            continue
        outcomes = [_fabric_clause(c, version) for c in clauses]
        if any(o is Satisfaction.VIOLATED for o in outcomes):
            results.append(Satisfaction.VIOLATED)
        elif any(o is Satisfaction.UNKNOWN for o in outcomes):
            results.append(Satisfaction.UNKNOWN)
        else:
            results.append(Satisfaction.SATISFIED)

    if any(r is Satisfaction.SATISFIED for r in results):
        return Satisfaction.SATISFIED
    if any(r is Satisfaction.UNKNOWN for r in results):
        return Satisfaction.UNKNOWN
    return Satisfaction.VIOLATED


# --------------------------------------------------------------------------
# Maven ranges (Forge, NeoForge)
# --------------------------------------------------------------------------

_MAVEN_INTERVAL = re.compile(r"^([\[\(])\s*([^,]*)\s*,\s*([^\]\)]*)\s*([\]\)])$")


def _maven_interval(spec: str, version: Version) -> Satisfaction:
    match = _MAVEN_INTERVAL.match(spec.strip())
    if match is None:
        return Satisfaction.UNKNOWN
    open_bracket, low_text, high_text, close_bracket = match.groups()

    if low_text.strip():
        low = parse_version(low_text)
        if low is None:
            return Satisfaction.UNKNOWN
        order = compare_versions(version, low)
        if order < 0 or (order == 0 and open_bracket == "("):
            return Satisfaction.VIOLATED

    if high_text.strip():
        high = parse_version(high_text)
        if high is None:
            return Satisfaction.UNKNOWN
        order = compare_versions(version, high)
        if order > 0 or (order == 0 and close_bracket == ")"):
            return Satisfaction.VIOLATED

    return Satisfaction.SATISFIED


def _maven_range(spec: str, version: Version) -> Satisfaction:
    spec = spec.strip()
    if not spec or spec in ("*", "any"):
        return Satisfaction.SATISFIED

    if not spec.startswith(("[", "(")):
        # Forge's convention: a bare version is a minimum, not an equality.
        wanted = parse_version(spec)
        if wanted is None:
            return Satisfaction.UNKNOWN
        return _yes(compare_versions(version, wanted) >= 0)

    # A comma at depth zero separates unioned intervals: "[1.0,2.0),[3.0,)".
    results = [_maven_interval(part, version) for part in _split_intervals(spec)]
    if any(r is Satisfaction.SATISFIED for r in results):
        return Satisfaction.SATISFIED
    if any(r is Satisfaction.UNKNOWN for r in results) or not results:
        return Satisfaction.UNKNOWN
    return Satisfaction.VIOLATED


def _split_intervals(spec: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in spec:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
            current.append(char)
            if depth == 0:
                parts.append("".join(current))
                current = []
            continue
        if depth > 0:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def satisfies(
    installed: str, spec: str, *, dialect: Dialect = Dialect.FABRIC
) -> Satisfaction:
    """Whether ``installed`` satisfies the range ``spec``.

    Returns ``UNKNOWN`` rather than guessing when either side cannot be parsed;
    callers must report that rather than treat it as met.
    """
    spec = (spec or "*").strip()
    if not spec or spec in ("*", "any"):
        return Satisfaction.SATISFIED
    if dialect is Dialect.BUKKIT:
        # plugin.yml has no version syntax; there is nothing to check.
        return Satisfaction.UNKNOWN

    version = parse_version(installed)
    if version is None:
        return Satisfaction.UNKNOWN

    if dialect is Dialect.MAVEN:
        return _maven_range(spec, version)
    return _fabric_range(spec, version)
