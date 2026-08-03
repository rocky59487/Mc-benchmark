"""Dependency graphs over a mod set, and the subset closures a bisect needs.

An arbitrary subset of a modpack is usually not installable, so a bisect has to
close each subset over its declared dependencies before launching it. Without
that, a culprit `bad` that needs `library` cannot be tested: `{bad}` fails to
launch and `{library}` fails to reproduce, and a search reading both as "does
not reproduce" reports an interaction between them.

Nothing here resolves versions. :mod:`mcbench.inspect` owns that over a
complete pack; this answers the narrower structural question of which mods a
subset must bring along.
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
        requires: mod id to the ids it hard-depends on, restricted to ids some
            mod in the pack provides. Ambient ids cannot be toggled.
        provided_by: id to the mod providing it, including ``provides`` aliases.
        unresolved: mod id to ids nothing provides. A subset containing such a
            mod is unlaunchable and the search is told so.
    """

    requires: Mapping[str, frozenset[str]]
    provided_by: Mapping[str, str]
    unresolved: Mapping[str, frozenset[str]]

    def dependencies_of(self, mod: str) -> frozenset[str]:
        return self.requires.get(mod, frozenset())

    def closure(self, subset: Sequence[str]) -> SubsetClosure:
        """Expand ``subset`` to everything it needs in order to load.

        Support is appended after the requested mods, sorted, so a probe label
        reads as the question asked and two runs produce identical audit records.
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

    ``candidates`` names the ids the bisect can toggle, normally the suite's
    mod list. A dependency outside that set but present in the pack is a
    fixture: installed in every probe regardless.

    Bundled jars count as present, since they are installed with their host.
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
