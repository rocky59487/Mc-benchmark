"""Version comparison and range evaluation, per loader dialect.

``mcbench inspect`` reads what mods declare about each other and decides whether
a pack will load. Until this module existed it decided that on *presence*: a
dependency was satisfied if some jar claimed the id, whatever version it was.
That gets the two most important cases exactly backwards. A pack declaring
``lib >=2`` and shipping ``lib 1`` was certified as fine and would not launch,
and a pack declaring ``breaks lib <2`` while shipping ``lib 3`` was reported as
broken and was fine. Both failures point the same way: the check said something
confident about a question it had not asked.

Two range dialects are implemented because the ecosystem has two.

**Fabric** uses npm-style semver ranges: ``*``, ``1.2.3``, ``>=1.2``, ``~1.2.3``
(patch-level changes), ``^1.2.3`` (compatible-with), ``1.2.x``, space-separated
conjunctions, and ``||`` for alternatives.

**Forge and NeoForge** use Maven version ranges: ``[1.0,2.0)`` half-open,
``[1.0,]`` unbounded above, bare ``1.0`` meaning "1.0 or later" by Forge's own
convention rather than "exactly 1.0" — a distinction that inverts the meaning of
most dependency declarations in the ecosystem if you get it wrong.

Version *comparison* is deliberately more forgiving than either specification.
Mod versions in the wild are not clean semver: ``1.21.1-0.6.0``, ``mc1.20.1-2.3``,
``4.0.0+1.21``, ``0.90.0+1.20.4`` and worse are all common. A strict parser would
reject a large fraction of real packs, and refusing to answer for most of the
ecosystem is not a safer failure than answering approximately — it just moves
the problem somewhere the operator cannot see it. So versions are compared
segment by segment, numerics numerically and the rest lexically, with a
pre-release rule that matches both specifications where they agree.
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

    ``UNKNOWN`` is a first-class answer and the reason this is not a boolean. A
    range this module cannot parse, or a version string with no comparable
    structure, must not silently become "satisfied" — that is the presence-only
    behaviour this module replaced. It also must not become "violated", which
    would fill a report with errors about packs that are fine. Reported as
    unknown, it becomes a warning that names what could not be decided.
    """

    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


_SEGMENT = re.compile(r"(\d+|[A-Za-z]+)")

#: A leading ``v``, as in ``v1.4``.
_PREFIX = re.compile(r"^(?:v|version)[-_. ]?(?=\d)", re.IGNORECASE)

#: The ``mc<game version>-`` tag Modrinth-published mods routinely prefix their
#: own version with: ``mc1.21-0.6.0`` is Sodium 0.6.0 for Minecraft 1.21, not
#: version 1.21 with a pre-release of 0.6.0. Stripped only when the marker is
#: literally present, because ``1.20.1-2.3`` without it is genuinely ambiguous
#: with a pre-release and guessing there would silently reorder real versions.
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
    """Parse a version string, or None when there is nothing comparable in it.

    Build metadata after ``+`` is discarded, as both specifications require: it
    is by definition not part of precedence. That matters here more than usual,
    because Fabric mods routinely encode the Minecraft version there
    (``0.92.0+1.20.1``) and comparing it would order mods by which game version
    they target rather than by which release they are.
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
        # Nothing numeric to anchor on — "unknown", "SNAPSHOT", a git hash. It
        # is not comparable, and pretending otherwise would order versions by
        # spelling.
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
        # A numeric segment sorts below an alphabetic one, matching semver's
        # rule for pre-release identifiers and the usual reading of "1.0" versus
        # "1.0rc".
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
        # Fabric treats a bare version as a prefix match, not equality: a
        # dependency on "1.2" is satisfied by 1.2.4. Reading it as equality
        # would report most working packs as broken.
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
        # Forge's documented convention: a bare version is a minimum, not an
        # equality. Reading it as equality would report almost every Forge pack
        # in existence as having an unsatisfied dependency.
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

    Returns :attr:`Satisfaction.UNKNOWN` rather than guessing when either side
    cannot be parsed. Every caller must then report the constraint as
    undecidable instead of quietly treating it as met, which is the whole point:
    the previous behaviour treated *every* constraint as met.
    """
    spec = (spec or "*").strip()
    if not spec or spec in ("*", "any"):
        return Satisfaction.SATISFIED
    if dialect is Dialect.BUKKIT:
        # plugin.yml has no version syntax at all. There is nothing to check,
        # and inventing a check would be worse than admitting that.
        return Satisfaction.UNKNOWN

    version = parse_version(installed)
    if version is None:
        return Satisfaction.UNKNOWN

    if dialect is Dialect.MAVEN:
        return _maven_range(spec, version)
    return _fabric_range(spec, version)
