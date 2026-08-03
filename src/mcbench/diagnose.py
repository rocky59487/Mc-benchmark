"""Culprit isolation: which mod in a pack is responsible for a regression.

Knowing a modpack is slow is not actionable. Knowing *which of its ninety mods*
makes it slow is. Testing all subsets is 2^90, so this implements delta debugging
(Zeller & Hildebrandt's ddmin) adapted to a benchmark's constraints.

Two adaptations matter, and they are why this is not just a binary search:

**The oracle is statistical, not deterministic.** A subset does not simply pass
or fail; it is slower by some amount with some confidence, and the same subset
measured twice can disagree. So the oracle returns three answers — regression,
no regression, and *inconclusive* — and an inconclusive probe is never treated as
evidence in either direction.

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

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Protocol, Sequence

__all__ = [
    "Outcome",
    "Probe",
    "Oracle",
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
    """The data does not support either answer. Never counted as evidence."""


@dataclass(frozen=True)
class Probe:
    """One oracle consultation, retained so the search is auditable."""

    subset: tuple[str, ...]
    outcome: Outcome
    detail: str = ""

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
    probes: list[Probe] = field(default_factory=list)
    converged: bool = False
    inconclusive_probes: int = 0
    note: str = ""

    @property
    def probe_count(self) -> int:
        return len(self.probes)

    @property
    def is_interaction(self) -> bool:
        """True when no single mod reproduces it — the pair or group is at fault.

        This is the finding a bisection alone would have missed, and usually the
        one worth reporting loudest: neither mod is individually broken, so
        neither author would find it alone.
        """
        return len(self.culprits) > 1

    def summary(self) -> str:
        if not self.converged:
            return f"did not converge after {self.probe_count} probes: {self.note}"
        if not self.culprits:
            return f"no culprit found in {self.probe_count} probes"
        who = ", ".join(self.culprits)
        if self.is_interaction:
            return (
                f"{len(self.culprits)} mods together cause the regression ({who}); "
                f"no smaller subset reproduces it"
            )
        return f"{who} causes the regression"


def isolate(
    mods: Sequence[str],
    oracle: Oracle,
    *,
    max_probes: int = 200,
    verify: bool = True,
) -> IsolationResult:
    """Find a minimal subset of ``mods`` that reproduces the regression.

    Implements ddmin. Returns a subset that is *1-minimal*: removing any single
    member stops it reproducing. That is a weaker guarantee than globally
    minimal, and deliberately so — global minimality costs exponentially more
    probes, and each probe here is several full game launches.

    Args:
        max_probes: Hard cap. Each probe is a full benchmark cell, so an
            unbounded search could run for days.
        verify: Confirm the full set actually regresses before searching. Without
            it, a search over a pack that is not slow at all wanders until the
            budget runs out and reports nothing useful.
    """
    result = IsolationResult()
    candidates = list(dict.fromkeys(mods))  # de-duplicate, preserve order

    if not candidates:
        result.converged = True
        result.note = "empty mod set"
        return result

    cache: dict[tuple[str, ...], Outcome] = {}

    def test(subset: Sequence[str]) -> Outcome:
        key = tuple(sorted(subset))
        if key in cache:
            return cache[key]
        if len(result.probes) >= max_probes:
            raise BudgetExhausted(
                f"stopped after {max_probes} probes; "
                f"raise max_probes or narrow the candidate set first"
            )
        outcome = oracle(list(subset))
        cache[key] = outcome
        result.probes.append(Probe(subset=tuple(subset), outcome=outcome))
        if outcome is Outcome.INCONCLUSIVE:
            result.inconclusive_probes += 1
        return outcome

    try:
        if verify:
            confirmed = test(candidates)
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
                result.note = (
                    "the full mod set does not reproduce a regression, so there "
                    "is nothing to isolate"
                )
                return result

        result.culprits = tuple(_ddmin(candidates, test))
        result.converged = True
    except BudgetExhausted as exc:
        result.note = str(exc)
        return result

    if result.inconclusive_probes:
        result.note = (
            f"{result.inconclusive_probes} probe(s) were inconclusive and were "
            f"treated as not reproducing; the true culprit set may be smaller. "
            f"Raising runs per cell would sharpen these."
        )
    return result


def _ddmin(candidates: list[str], test: Callable[[Sequence[str]], Outcome]) -> list[str]:
    """The ddmin core: split, test parts, test complements, refine."""
    if len(candidates) <= 1:
        return candidates

    granularity = 2
    current = candidates

    while len(current) > 1:
        chunks = _split(current, granularity)

        # Does one chunk reproduce it alone? Narrows fastest when a single mod is
        # at fault.
        reduced = None
        for chunk in chunks:
            if chunk and test(chunk) is Outcome.REGRESSION:
                reduced = chunk
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
        for index, chunk in enumerate(chunks):
            complement = [m for m in current if m not in set(chunk)]
            if complement and test(complement) is Outcome.REGRESSION:
                narrowed = complement
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
) -> dict[str, Outcome]:
    """Test the set with each mod removed in turn.

    Costs exactly ``len(mods)`` probes and answers a different question than
    :func:`isolate`: not "what is the minimal culprit" but "what does each mod
    contribute". Useful for a health check across a whole pack, where the goal is
    a ranked contribution list rather than a single verdict.

    A mod whose removal makes the regression disappear is individually
    responsible. If removing *no single mod* helps, the cause is an interaction
    and :func:`isolate` is the right tool.
    """
    outcomes: dict[str, Outcome] = {}
    for mod in mods:
        remaining = [m for m in mods if m != mod]
        outcomes[mod] = oracle(remaining) if remaining else Outcome.CLEAN
    return outcomes
