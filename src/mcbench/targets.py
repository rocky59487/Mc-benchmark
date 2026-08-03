"""Targets, capabilities, and command dialects.

This is what makes "write the scenario once, run it everywhere" true rather than
aspirational. A scenario is a platform-neutral description of work; a
:class:`Target` is a concrete ``(platform, version, mods)`` combination; and a
:class:`Dialect` knows how that target actually spells things.

The problem it solves is that the command surface is stable but not *uniform*:

- ``/item replace block`` is 1.17 and later; before that it was ``/replaceitem``.
- ``/forceload`` arrived in 1.13.1; there is no equivalent before it.
- ``/tick warp`` is vanilla from 1.20.3, but on older versions it needs Carpet,
  which does not exist for plugin platforms at all.
- Paper has no client, so a rendering scenario cannot run there in any spelling.

Compiling one scenario against every target with a single hard-coded dialect
would therefore emit commands that fail — or worse, quietly do nothing — on some
of them. The governing rule here is the same one that governs the whole project:
**a target that cannot express a scenario must refuse it loudly**, because a
scenario that half-executes still produces numbers, and those numbers look
entirely valid.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering

from .config import Loader

__all__ = [
    "Version",
    "Capability",
    "Target",
    "Dialect",
    "UnsupportedTarget",
    "FLATTENING_VERSION",
]

#: The 1.13 "flattening" renamed essentially every block and item. Scenarios are
#: written with modern identifiers, so compiling one for an older target would
#: place the wrong blocks rather than fail — the worst possible outcome. Older
#: targets are refused until an identifier mapping exists.
FLATTENING_VERSION = "1.13"


class UnsupportedTarget(ValueError):
    """A target cannot express what a scenario requires."""


@total_ordering
@dataclass(frozen=True)
class Version:
    """A comparable Minecraft version.

    Handles both numbering schemes in one ordering. Classic releases are
    ``1.MAJOR.MINOR`` while releases from 2026 onward are year-based
    (``26.1.2``), and because 26 > 1 a plain tuple comparison already orders them
    correctly — the new scheme sorts after every ``1.x`` without a special case.

    Snapshots (``24w14a``) are not orderable against releases in any meaningful
    way and are treated as unknown rather than guessed at.
    """

    raw: str
    parts: tuple[int, ...] = field(compare=True, default=())
    snapshot: bool = False

    @classmethod
    def parse(cls, raw: str) -> Version:
        text = str(raw).strip()
        if re.fullmatch(r"\d{2}w\d{2}[a-z]", text):
            return cls(raw=text, parts=(), snapshot=True)

        # Strip pre-release and release-candidate suffixes; they order just
        # before their release and never matter for capability gating.
        base = re.split(r"[-+]|(?:\s)", text)[0]
        pieces = base.split(".")
        numbers: list[int] = []
        for piece in pieces:
            match = re.match(r"^(\d+)", piece)
            if not match:
                break
            numbers.append(int(match.group(1)))
        if not numbers:
            return cls(raw=text, parts=(), snapshot=True)
        return cls(raw=text, parts=tuple(numbers))

    @property
    def known(self) -> bool:
        return bool(self.parts)

    def _key(self, length: int = 4) -> tuple[int, ...]:
        padded = list(self.parts) + [0] * length
        return tuple(padded[:length])

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: Version) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def at_least(self, other: str | Version) -> bool:
        """True when this version is ``other`` or newer.

        An unparseable version returns False for every gate. That is the
        conservative direction: it makes a capability look unavailable and
        produces a clear refusal, rather than assuming support and emitting a
        command the target rejects.
        """
        if not self.known:
            return False
        target = other if isinstance(other, Version) else Version.parse(other)
        return self >= target

    def __str__(self) -> str:
        return self.raw


class Capability(str, Enum):
    """Something a target either can or cannot do."""

    CLIENT = "client"
    """Has a client, so rendering scenarios are possible at all."""
    FORCELOAD = "forceload"
    """``/forceload`` — chunk loading without a player. Since 1.13.1."""
    TICK_WARP = "tick_warp"
    """Running ticks as fast as the CPU allows. Vanilla 1.20.3+, or Carpet."""
    ITEM_REPLACE_BLOCK = "item_replace_block"
    """Placing items into a block's inventory, in either spelling."""
    SIMULATION_DISTANCE = "simulation_distance"
    """Independent simulation distance. Since 1.18."""
    MODERN_IDENTIFIERS = "modern_identifiers"
    """Post-flattening block and item names. Since 1.13."""
    ATTRIBUTE_COMMAND = "attribute_command"
    """``/attribute``. Since 1.16."""


@dataclass(frozen=True)
class Target:
    """A concrete platform, version, and set of loaded mods."""

    platform: Loader
    minecraft_version: str
    mods: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, spec: str, *, mods: Iterable[str] = ()) -> Target:
        """Parse ``"fabric:1.21.1"`` into a target."""
        platform_part, _, version = spec.partition(":")
        if not version:
            raise UnsupportedTarget(
                f"target {spec!r} must be 'platform:version', e.g. 'fabric:1.21.1'"
            )
        try:
            platform = Loader(platform_part.strip().lower())
        except ValueError:
            raise UnsupportedTarget(
                f"unknown platform {platform_part!r}; "
                f"expected one of {[p.value for p in Loader]}"
            ) from None
        return cls(
            platform=platform,
            minecraft_version=version.strip(),
            mods=frozenset(m.lower() for m in mods),
        )

    @property
    def version(self) -> Version:
        return Version.parse(self.minecraft_version)

    @property
    def has_carpet(self) -> bool:
        return "carpet" in self.mods

    def with_mods(self, mods: Iterable[str]) -> Target:
        return Target(
            platform=self.platform,
            minecraft_version=self.minecraft_version,
            mods=frozenset(m.lower() for m in mods),
        )

    def __str__(self) -> str:
        return f"{self.platform.value}:{self.minecraft_version}"


class Dialect:
    """How a specific target spells things, and what it can do at all.

    The compiler asks a dialect rather than hard-coding command text, so adding a
    platform or supporting an older version changes this class and nothing else.
    """

    def __init__(self, target: Target) -> None:
        self.target = target
        self.version = target.version

    # -- capabilities ----------------------------------------------------

    def supports(self, capability: Capability) -> bool:
        version = self.version
        platform = self.target.platform

        if capability is Capability.CLIENT:
            # Plugin platforms are server-only; there is no client to render on.
            return not platform.is_plugin_platform

        if capability is Capability.MODERN_IDENTIFIERS:
            return version.at_least(FLATTENING_VERSION)

        if capability is Capability.FORCELOAD:
            return version.at_least("1.13.1")

        if capability is Capability.SIMULATION_DISTANCE:
            return version.at_least("1.18")

        if capability is Capability.ATTRIBUTE_COMMAND:
            return version.at_least("1.16")

        if capability is Capability.ITEM_REPLACE_BLOCK:
            # Both spellings exist across the supported range, so the capability
            # is present even though the command text differs.
            return version.at_least(FLATTENING_VERSION)

        if capability is Capability.TICK_WARP:
            if version.at_least("1.20.3"):
                return True  # vanilla /tick warp
            # Carpet provides it on mod loaders only; there is no plugin build.
            return self.target.has_carpet and not platform.is_plugin_platform

        return False

    def missing(self, capabilities: Iterable[Capability]) -> list[Capability]:
        return [c for c in capabilities if not self.supports(c)]

    def explain(self, capability: Capability) -> str:
        """Why a capability is unavailable, and what would provide it."""
        version = self.version
        platform = self.target.platform

        if capability is Capability.CLIENT:
            return (
                f"{platform.value} is a server platform with no client, so "
                f"rendering cannot be measured on it"
            )
        if capability is Capability.MODERN_IDENTIFIERS:
            return (
                f"Minecraft {version} predates the 1.13 flattening, which renamed "
                f"essentially every block and item. Scenarios use modern "
                f"identifiers, so compiling for this target would place the wrong "
                f"blocks rather than fail — an identifier mapping is needed first"
            )
        if capability is Capability.FORCELOAD:
            return f"/forceload was added in 1.13.1; this target is {version}"
        if capability is Capability.SIMULATION_DISTANCE:
            return (
                f"independent simulation distance was added in 1.18; "
                f"this target is {version}"
            )
        if capability is Capability.TICK_WARP:
            if platform.is_plugin_platform:
                return (
                    f"tick warp needs vanilla /tick (1.20.3+) — this target is "
                    f"{version}, and Carpet has no build for {platform.value}"
                )
            return (
                f"tick warp needs vanilla /tick (1.20.3+) or Carpet — this target "
                f"is {version} and Carpet is not in the variant's mod list"
            )
        return f"{capability.value} is unavailable on {self.target}"

    # -- command spelling ------------------------------------------------

    def item_replace_block(
        self, x: int, y: int, z: int, slot: str, item: str, count: int
    ) -> str:
        if self.version.at_least("1.17"):
            return f"item replace block {x} {y} {z} {slot} with {item} {count}"
        # Pre-1.17 spelling. Slot names are the same; only the verb moved.
        return f"replaceitem block {x} {y} {z} {slot} {item} {count}"

    def tick_warp(self, ticks: int) -> str:
        """Run ``ticks`` ticks as fast as the CPU allows."""
        if self.version.at_least("1.20.3"):
            return f"tick warp {ticks}"
        return f"tick warp {ticks}"  # Carpet uses the same syntax

    def tick_warp_stop(self) -> str:
        return "tick warp 0" if self.version.at_least("1.20.3") else "tick warp 0"

    def weather(self, kind: str) -> str:
        return f"weather {kind}"

    def difficulty(self, level: str) -> str:
        return f"difficulty {level}"

    def time_set(self, ticks: int) -> str:
        return f"time set {ticks}"

    @property
    def supports_client_scenarios(self) -> bool:
        return self.supports(Capability.CLIENT)

    def __str__(self) -> str:
        return f"Dialect({self.target})"


def required_capabilities(
    *,
    side: str,
    uses_tick_warp: bool,
    ops: Iterable[str],
) -> set[Capability]:
    """Capabilities a scenario needs, derived from what it actually does.

    Derived from the compiled action set rather than declared by hand, so a
    scenario cannot forget to mention a requirement and appear to run on a target
    that silently drops half of it.
    """
    needed: set[Capability] = {Capability.MODERN_IDENTIFIERS}

    if side in ("client", "both"):
        needed.add(Capability.CLIENT)
    if uses_tick_warp:
        needed.add(Capability.TICK_WARP)

    op_set = set(ops)
    if op_set & {"generate_chunks", "load_chunks", "unload_chunks"}:
        needed.add(Capability.FORCELOAD)
    if "set_simulation_distance" in op_set:
        needed.add(Capability.SIMULATION_DISTANCE)
    if "place_structure" in op_set:
        # Templates pre-load container inventories.
        needed.add(Capability.ITEM_REPLACE_BLOCK)

    return needed
