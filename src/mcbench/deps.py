"""Dependency graphs over a mod set, and the subset closures a bisect needs.

A bisect launches subsets of a modpack, and an arbitrary subset of a modpack is
usually not installable. This module is what turns "these five mods" into "these
five mods and the four libraries they need", so that a probe measures a
configuration the game will actually load.

The failure this prevents is specific and it is the reason issue #11 exists. A
mod ``bad`` requires ``library``. Testing ``{bad}`` alone fails to launch, and
testing ``{library}`` alone fails to reproduce, so a search that reads both
failures as "does not reproduce" concludes that neither mod is individually at
fault and reports an *interaction* between them. Two mod authors then receive a
bug report about a conflict that does not exist, and the actual single-mod fault
goes unfixed.

Nothing here resolves versions. That is deliberate: :mod:`mcbench.inspect` owns
version-range evaluation over a *complete* pack, and re-deciding compatibility
per subset would let a bisect quietly install a combination the pack's own
metadata rejects. What this module answers is narrower and purely structural —
given these mods are already known to work together, which of them does a subset
have to bring along.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .diagnose import SubsetClosure
from .inspect import ModMetadata, flatten_mods, is_ambient

__all__ = [
    "DependencyGraph",
    "graph_from_metadata",
]


@dataclass(frozen=True)
class DependencyGraph:
    """Which mods a mod needs, restricted to the pack under test.

    Args:
        requires: mod id to the ids it hard-depends on. Only ids that some mod
            in the pack provides are retained; anything supplied by the
            environment (``minecraft``, the loader itself) is not a bisect
            candidate and cannot be toggled.
        provided_by: id to the mod that provides it, covering ``provides``
            aliases so a dependency on a module resolves to the jar shipping it.
        unresolved: mod id to the ids it needs that nothing in the pack provides
            and the environment does not supply. A subset containing such a mod
            cannot be launched, and a search must be told so rather than
            discovering it as a mysterious run of failed launches.
    """

    requires: Mapping[str, frozenset[str]]
    provided_by: Mapping[str, str]
    unresolved: Mapping[str, frozenset[str]]

    def dependencies_of(self, mod: str) -> frozenset[str]:
        return self.requires.get(mod, frozenset())

    def closure(self, subset: Sequence[str]) -> SubsetClosure:
        """Expand ``subset`` to everything it needs in order to load.

        Support mods are appended after the requested ones so that a caller
        rendering the set shows the suspects first, and the ordering is
        deterministic (sorted) so two runs of the same search produce byte-equal
        audit records.
        """
        requested = list(dict.fromkeys(subset))
        members = list(requested)
        seen = set(members)
        missing: set[str] = set()

        frontier = list(requested)
        while frontier:
            mod = frontier.pop()
            missing.update(self.unresolved.get(mod, frozenset()))
            for needed in sorted(self.dependencies_of(mod)):
                owner = self.provided_by.get(needed)
                if owner is None:
                    missing.add(needed)
                    continue
                if owner in seen:
                    continue
                seen.add(owner)
                members.append(owner)
                frontier.append(owner)

        support = tuple(sorted(m for m in members if m not in set(requested)))
        return SubsetClosure(
            subset=tuple(requested),
            # Requested first, then support, so a probe label reads as the
            # question that was asked rather than as the pile that was installed.
            members=tuple(requested) + support,
            support=support,
            missing=tuple(sorted(missing)),
        )


def graph_from_metadata(
    mods: Iterable[ModMetadata],
    *,
    candidates: Iterable[str] | None = None,
) -> DependencyGraph:
    """Build a graph from jars already read by :mod:`mcbench.inspect`.

    ``candidates`` names the ids the bisect can actually toggle — normally the
    suite's mod list. A dependency on something outside that set but present in
    the pack is not a missing dependency, it is a fixture: it is installed in
    every probe regardless, so it neither needs adding nor counts as absent.

    Bundled jars count as present. A library shipped inside its dependent is
    installed whenever that dependent is, so a subset containing the dependent
    already satisfies the dependency and adding a separate copy would be a
    duplicate install rather than a fix.
    """
    mods = flatten_mods(mods)
    toggleable = set(candidates) if candidates is not None else {m.mod_id for m in mods}

    provided_by: dict[str, str] = {}
    for mod in mods:
        if not mod.mod_id:
            continue
        provided_by.setdefault(mod.mod_id, mod.mod_id)
        for alias in mod.provides:
            provided_by.setdefault(alias, mod.mod_id)

    requires: dict[str, frozenset[str]] = {}
    unresolved: dict[str, frozenset[str]] = {}
    for mod in mods:
        needed: set[str] = set()
        absent: set[str] = set()
        for dependency in mod.depends:
            if is_ambient(dependency):
                continue
            owner = provided_by.get(dependency)
            if owner is None:
                absent.add(dependency)
            elif owner in toggleable and owner != mod.mod_id:
                needed.add(dependency)
        if needed:
            requires[mod.mod_id] = frozenset(needed)
        if absent:
            unresolved[mod.mod_id] = frozenset(absent)

    return DependencyGraph(
        requires=requires,
        provided_by={k: v for k, v in provided_by.items() if v in toggleable},
        unresolved=unresolved,
    )
