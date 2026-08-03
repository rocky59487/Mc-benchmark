"""Culprit isolation: which mod in a pack is responsible for a regression.

Knowing a modpack is slow is not actionable. Knowing *which of its ninety mods*
makes it slow is. Testing all subsets is 2^90, so this implements delta debugging
(Zeller & Hildebrandt's ddmin) adapted to a benchmark's constraints.

Three adaptations matter, and they are why this is not just a binary search:

**The oracle is statistical, not deterministic.** A subset does not simply pass
or fail; it is slower by some amount with some confidence, and the same subset
measured twice can disagree. So the oracle returns four answers — regression, no
regression, *inconclusive*, and *invalid* — and only the first two are evidence.

**A subset must be a loadable configuration.** Mods have dependencies, and an
arbitrary half of a modpack usually is not installable. A culprit ``bad`` that
requires ``library`` cannot be tested on its own: ``{bad}`` fails to launch for
want of its library, ``{library}`` fails to reproduce because it is not the
culprit, and a search that reads either failure as "does not reproduce" concludes
the two interact — a false interaction claim about a single-mod fault. So every
subset is closed over its declared dependencies before it is launched, and the
support mods added that way are reported separately from the suspects.

**The culprit is often a combination.** Two mods can each be harmless and be
catastrophic together, which is precisely the interaction effect the rest of this
project exists to measure. A plain bisection breaks on that case: it splits the
pair, sees neither half regress, and concludes nothing is wrong. ddmin's
complement phase is what handles it, and the ``granularity`` escalation is what
finds culprits that a single split keeps separating.

Every probe costs a full benchmark cell — several fresh game launches — so the
search is budgeted and reports what it spent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

__all__ = [
    "Outcome",
    "Probe",
    "Oracle",
    "SubsetClosure",
    "IsolationResult",
    "BudgetExhausted",
    "isolate",
    "leave_one_out",
]


class Outcome(str, Enum):
    """What a probe of one mod subset concluded."""

    REGRESSION = "regression"
    """This subset is measurably worse than the baseline, beyond the ROPE."""
    CLEAN = "clean"
    """This subset is not measurably worse."""
    INCONCLUSIVE = "inconclusive"
    """The runs were made and did not settle the question. Never evidence."""
    INVALID = "invalid"
    """The subset is not a configuration that can be measured at all — a
    dependency it needs is absent, or every launch of it failed while the
    baseline launched fine in the same probe.

    Kept distinct from ``CLEAN`` because the difference is the whole failure this
    search has to avoid. "It did not reproduce the regression" and "it never
    ran" look identical to a caller that only checks ``is not REGRESSION``, and
    conflating them lets an unlaunchable half of a pack exonerate the mod it
    contains.
    """

    @property
    def is_evidence(self) -> bool:
        """Whether this outcome may be used to narrow or to claim minimality."""
        return self in (Outcome.REGRESSION, Outcome.CLEAN)


@dataclass(frozen=True)
class SubsetClosure:
    """A candidate subset expanded to something that can actually be launched.

    ``members`` is what gets installed; ``subset`` is what is under suspicion.
    The two are reported separately for a practical reason: a support mod pulled
    in to satisfy a dependency is present in the instance and could in principle
    be contributing, and a result that quietly folded it into the culprit list
    would name a mod the search never independently implicated.
    """

    subset: tuple[str, ...]
    members: tuple[str, ...]
    support: tuple[str, ...] = ()
    """Mods added purely to satisfy dependencies of ``subset``."""
    missing: tuple[str, ...] = ()
    """Required ids that no candidate provides; non-empty means unlaunchable."""

    @property
    def satisfiable(self) -> bool:
        return not self.missing


def identity_closure(subset: Sequence[str]) -> SubsetClosure:
    """Closure for a mod set with no known dependency metadata.

    Used when the caller has nothing to build a graph from. It assumes every
    subset is launchable, which is the old behaviour and is unsafe on a real
    modpack — :func:`isolate` says so in its result note rather than letting the
    assumption pass unremarked.
    """
    members = tuple(dict.fromkeys(subset))
    return SubsetClosure(subset=members, members=members)


@dataclass(frozen=True)
class Probe:
    """One oracle consultation, retained so the search is auditable."""

    subset: tuple[str, ...]
    outcome: Outcome
    detail: str = ""
    tested: tuple[str, ...] = ()
    """What was actually installed — the dependency closure of ``subset``."""
    support: tuple[str, ...] = ()
    """Mods present only to satisfy a dependency."""

    @property
    def size(self) -> int:
        return len(self.subset)


class Oracle(Protocol):
    """Tests whether a mod subset reproduces the regression."""

    def __call__(self, subset: Sequence[str]) -> Outcome: ...


class BudgetExhausted(RuntimeError):
    """The search ran out of probes before converging."""


@dataclass
class IsolationResult:
    """What the search concluded, and what it cost."""

    culprits: tuple[str, ...] = ()
    support: tuple[str, ...] = ()
    """Dependencies installed alongside the culprits so they could be launched.
    Present in the instance, not implicated by the search."""
    probes: list[Probe] = field(default_factory=list)
    converged: bool = False
    inconclusive_probes: int = 0
    invalid_probes: int = 0
    minimal: bool = False
    """True only when every single removal from the culprit set was tested and
    stopped reproducing. False means the set may not be minimal — which is not
    the same as being wrong, and is reported as the weaker claim it is."""
    revalidated: bool = False
    """True when the final candidate was re-measured and reproduced again."""
    note: str = ""

    @property
    def probe_count(self) -> int:
        return len(self.probes)

    @property
    def unresolved_probes(self) -> int:
        """Probes that produced no evidence in either direction."""
        return self.inconclusive_probes + self.invalid_probes

    @property
    def is_interaction(self) -> bool:
        """True when no single mod reproduces it — the pair or group is at fault.

        Only claimed once the set is confirmed minimal. Without that, a group of
        two could just as easily be one culprit and one passenger the search
        never managed to test apart, and "these two mods interact" is precisely
        the kind of finding that gets reported to two mod authors.
        """
        return len(self.culprits) > 1 and self.minimal

    def summary(self) -> str:
        if not self.converged:
            return f"did not converge after {self.probe_count} probes: {self.note}"
        if not self.culprits:
            return f"no culprit found in {self.probe_count} probes"
        who = ", ".join(self.culprits)
        if len(self.culprits) > 1:
            claim = (
                f"{len(self.culprits)} mods together cause the regression ({who}); "
                f"no smaller subset reproduces it"
                if self.minimal
                else f"the regression narrows to {who}, but minimality could not "
                f"be established"
            )
            return claim
        return (
            f"{who} causes the regression"
            if self.minimal
            else f"the regression narrows to {who} (minimality unconfirmed)"
        )


def isolate(
    mods: Sequence[str],
    oracle: Oracle,
    *,
    max_probes: int = 200,
    verify: bool = True,
    closure: Callable[[Sequence[str]], SubsetClosure] = identity_closure,
    confirm_minimality: bool = True,
) -> IsolationResult:
    """Find a minimal subset of ``mods`` that reproduces the regression.

    Implements ddmin over *dependency-closed* subsets. Returns a subset that is
    *1-minimal*: removing any single member stops it reproducing. That is a
    weaker guarantee than globally minimal, and deliberately so — global
    minimality costs exponentially more probes, and each probe here is several
    full game launches.

    Minimality is now something the result claims only when it was checked.
    ddmin's own stopping condition is "no split narrowed anything", and on a
    statistical oracle that can be reached by a run of unresolved probes rather
    than by having found the answer. So the candidate is verified explicitly at
    the end, and a set that could not be verified is reported as unconfirmed
    rather than announced as minimal.

    Args:
        max_probes: Hard cap. Each probe is a full benchmark cell, so an
            unbounded search could run for days.
        verify: Confirm the full set actually regresses before searching. Without
            it, a search over a pack that is not slow at all wanders until the
            budget runs out and reports nothing useful.
        closure: Expands a candidate subset into a launchable configuration.
            Defaults to installing exactly the subset, which is only correct for
            mod sets with no dependencies between them.
        confirm_minimality: Test every single removal from the final candidate,
            and re-measure the candidate itself. Costs up to ``len(culprits)+1``
            further probes and is what turns "ddmin stopped here" into a claim.
    """
    result = IsolationResult()
    candidates = list(dict.fromkeys(mods))  # de-duplicate, preserve order

    if not candidates:
        result.converged = True
        result.minimal = True
        result.note = "empty mod set"
        return result

    cache: dict[tuple[str, ...], Outcome] = {}

    def close(subset: Sequence[str]) -> SubsetClosure:
        return closure(list(dict.fromkeys(subset)))

    def test(subset: Sequence[str]) -> Outcome:
        resolved = close(subset)
        # Keyed on what would actually be installed, not on what was asked for.
        # Two different suspect sets with the same closure are the same launch,
        # and paying for it twice would burn a benchmark cell to learn nothing.
        key = tuple(sorted(resolved.members))
        if resolved.satisfiable and key in cache:
            return cache[key]
        if len(result.probes) >= max_probes:
            raise BudgetExhausted(
                f"stopped after {max_probes} probes; "
                f"raise max_probes or narrow the candidate set first"
            )

        if not resolved.satisfiable:
            # Never launched: there is no configuration here to measure. Recording
            # it is essential — an unsatisfiable subset is why a search fails to
            # narrow, and the operator needs to see that rather than infer it.
            outcome = Outcome.INVALID
            detail = "unsatisfiable: missing " + ", ".join(resolved.missing)
        else:
            outcome = oracle(list(resolved.members))
            detail = ""
            cache[key] = outcome

        result.probes.append(Probe(
            subset=tuple(resolved.subset),
            outcome=outcome,
            detail=detail,
            tested=resolved.members,
            support=resolved.support,
        ))
        if outcome is Outcome.INCONCLUSIVE:
            result.inconclusive_probes += 1
        elif outcome is Outcome.INVALID:
            result.invalid_probes += 1
        return outcome

    try:
        if verify:
            confirmed = test(candidates)
            if confirmed is Outcome.INVALID:
                result.converged = False
                result.note = (
                    "the full mod set could not be launched, so there is no "
                    "regression to isolate. Run `mcbench inspect` on the pack: "
                    "a missing dependency or a declared conflict has to be fixed "
                    "before anything can be measured"
                )
                return result
            if confirmed is Outcome.INCONCLUSIVE:
                # "We cannot tell" is not "there is nothing wrong". Reporting the
                # latter would send someone away believing their pack is fine,
                # which is the conflation this project refuses everywhere else.
                # Searching on regardless would be worse: every subsequent probe
                # would be measured against an effect we never established.
                result.converged = False
                result.note = (
                    "the full mod set's regression could not be confirmed — the "
                    "measurement was inconclusive, not clean. Raise runs per cell "
                    "or pick a scenario with a larger effect, then isolate again"
                )
                return result
            if confirmed is Outcome.CLEAN:
                result.converged = True
                result.minimal = True
                result.note = (
                    "the full mod set does not reproduce a regression, so there "
                    "is nothing to isolate"
                )
                return result

        current = _ddmin(candidates, test, close)
        result.converged = True

        if confirm_minimality:
            _confirm(result, current, test, close)
        else:
            result.culprits = tuple(current)
    except BudgetExhausted as exc:
        result.culprits = result.culprits or tuple(candidates)
        result.note = str(exc)
        return result

    _explain(result, closure is identity_closure)
    return result


def _confirm(
    result: IsolationResult,
    culprits: Sequence[str],
    test: Callable[[Sequence[str]], Outcome],
    close: Callable[[Sequence[str]], SubsetClosure],
) -> None:
    """Re-measure the candidate and check every single removal.

    Two separate claims are being checked and they fail differently. Re-measuring
    the candidate guards against a regression that was never there: ddmin follows
    REGRESSION verdicts, and one lucky verdict early can steer the whole search.
    Testing each removal is what 1-minimality actually means, and it is the claim
    a report makes when it names a mod.

    A removal that comes back unresolved is fatal to the minimality claim but not
    to the result: the candidate still reproduces, we simply cannot say nothing
    smaller does.

    Members that cannot be removed at all — because another member declares a
    dependency on them — are separated out here rather than counted against
    minimality. They are support, and the search never had the option of testing
    the set without them.
    """
    installed = list(close(culprits).members)
    if not installed:
        result.culprits = ()
        result.minimal = True
        return

    if test(installed) is not Outcome.REGRESSION:
        result.culprits = tuple(installed)
        result.converged = False
        result.note = (
            "the candidate set did not reproduce the regression when it was "
            "re-measured, so the search followed a verdict that did not hold up. "
            "Raise runs per probe and isolate again"
        )
        return
    result.revalidated = True

    # A member that the closure puts straight back is required by another
    # member: it is scaffolding, not a suspect the search chose.
    support = [
        mod for mod in installed
        if mod in set(close([m for m in installed if m != mod]).members)
    ]
    suspects = [mod for mod in installed if mod not in set(support)]
    result.culprits = tuple(suspects or installed)
    result.support = tuple(support)

    if len(result.culprits) == 1 and not support:
        # Removing the only member leaves the empty set, which is the baseline by
        # construction and reproduces nothing. Nothing to launch.
        result.minimal = True
        return

    unresolved: list[str] = []
    for mod in result.culprits:
        reduced = [m for m in installed if m != mod]
        outcome = test(reduced)
        if outcome is Outcome.REGRESSION:
            # A smaller set still reproduces: whatever ddmin returned, this is
            # not 1-minimal, and saying so beats quietly shrinking the answer
            # after the fact on a single probe.
            result.minimal = False
            result.note = (
                f"removing {mod} left a set that still reproduces, so the "
                f"reported set is not minimal; re-run the search"
            )
            return
        if not outcome.is_evidence:
            unresolved.append(mod)

    if unresolved:
        result.minimal = False
        result.note = (
            f"minimality is unconfirmed: removing {', '.join(unresolved)} could "
            f"not be measured conclusively, so a smaller culprit set cannot be "
            f"ruled out"
        )
        return

    if support:
        result.minimal = False
        result.note = (
            f"every removable mod was tested, but {', '.join(support)} could not "
            f"be removed without making the set unlaunchable, so it remains "
            f"untested as a cause"
        )
        return

    result.minimal = True


def _explain(result: IsolationResult, assumed_no_dependencies: bool) -> None:
    """Attach the caveats a reader needs, without overwriting a specific one."""
    notes: list[str] = []
    if result.note:
        notes.append(result.note)

    if result.invalid_probes:
        notes.append(
            f"{result.invalid_probes} probe(s) could not be launched at all and "
            f"were recorded as invalid, never as clean"
        )
    if result.inconclusive_probes:
        notes.append(
            f"{result.inconclusive_probes} probe(s) were inconclusive; they did "
            f"not narrow the search and did not exonerate anything. Raising runs "
            f"per probe would sharpen these"
        )
    if assumed_no_dependencies and len(result.culprits) > 1:
        notes.append(
            "no dependency metadata was supplied, so every subset was assumed "
            "launchable. On a pack with real dependencies that assumption can "
            "turn one culprit and its library into an apparent interaction"
        )
    if result.support:
        notes.append(
            "installed to satisfy dependencies, not implicated: "
            + ", ".join(result.support)
        )
    result.note = ". ".join(notes)


def _ddmin(
    candidates: list[str],
    test: Callable[[Sequence[str]], Outcome],
    close: Callable[[Sequence[str]], SubsetClosure],
) -> list[str]:
    """The ddmin core: split, test parts, test complements, refine.

    Only ``REGRESSION`` narrows. Every other outcome — clean, inconclusive, or
    an unlaunchable subset — leaves the current set alone, which means an
    unresolved probe costs a launch and buys nothing rather than silently
    steering the search. The caller counts those and reports them.

    The working set is kept **dependency-closed** throughout, and a narrowing is
    accepted only when it strictly shrinks that closed set. Both rules exist for
    the same reason. When a chunk that reproduces needs a library from the other
    half, what was launched is the chunk *plus the library* — so narrowing to the
    chunk alone would drop a mod that was installed and never ruled out. And when
    removing a chunk of pure libraries re-adds every one of them, the "smaller"
    set is the set we already had; accepting it would loop forever on a search
    that is not progressing.
    """
    current = list(close(candidates).members) or list(candidates)
    if len(current) <= 1:
        return current

    granularity = 2

    while len(current) > 1:
        chunks = _split(current, granularity)

        # Does one chunk reproduce it alone? Narrows fastest when a single mod is
        # at fault.
        reduced = None
        for chunk in chunks:
            if not chunk:
                continue
            if test(chunk) is not Outcome.REGRESSION:
                continue
            expanded = list(close(chunk).members)
            if len(expanded) < len(current):
                reduced = expanded
                break
        if reduced is not None:
            current = reduced
            granularity = 2
            continue

        # Does removing one chunk still reproduce it? This is the phase that
        # survives interactions: when a culprit pair straddles a split, neither
        # half reproduces alone, but every complement that keeps the pair
        # together does.
        narrowed = None
        for chunk in chunks:
            complement = [m for m in current if m not in set(chunk)]
            if not complement:
                continue
            if test(complement) is not Outcome.REGRESSION:
                continue
            expanded = list(close(complement).members)
            if len(expanded) < len(current):
                narrowed = expanded
                granularity = max(granularity - 1, 2)
                break
        if narrowed is not None:
            current = narrowed
            continue

        # Neither phase narrowed anything. A finer split may separate a culprit
        # group that the current one keeps straddling.
        if granularity >= len(current):
            break
        granularity = min(granularity * 2, len(current))

    return current


def _split(items: Sequence[str], parts: int) -> list[list[str]]:
    """Split into ``parts`` near-equal chunks, dropping empty ones."""
    parts = max(1, min(parts, len(items)))
    size, remainder = divmod(len(items), parts)
    chunks: list[list[str]] = []
    start = 0
    for index in range(parts):
        length = size + (1 if index < remainder else 0)
        if length:
            chunks.append(list(items[start : start + length]))
        start += length
    return chunks


def leave_one_out(
    mods: Sequence[str],
    oracle: Oracle,
    *,
    closure: Callable[[Sequence[str]], SubsetClosure] = identity_closure,
) -> dict[str, Outcome]:
    """Test the set with each mod removed in turn.

    Costs exactly ``len(mods)`` probes and answers a different question than
    :func:`isolate`: not "what is the minimal culprit" but "what does each mod
    contribute". Useful for a health check across a whole pack, where the goal is
    a ranked contribution list rather than a single verdict.

    A mod whose removal makes the regression disappear is individually
    responsible. If removing *no single mod* helps, the cause is an interaction
    and :func:`isolate` is the right tool.

    Removing a mod that others depend on leaves a set that will not load. That
    is reported as ``INVALID`` rather than as the mod mattering enormously,
    which is what a launch failure would otherwise look like.
    """
    outcomes: dict[str, Outcome] = {}
    for mod in mods:
        remaining = [m for m in mods if m != mod]
        if not remaining:
            outcomes[mod] = Outcome.CLEAN
            continue
        resolved = closure(remaining)
        if not resolved.satisfiable:
            outcomes[mod] = Outcome.INVALID
            continue
        outcomes[mod] = oracle(list(resolved.members))
    return outcomes
