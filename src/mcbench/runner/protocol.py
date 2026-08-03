"""The probe protocol: the contract between the in-game mod and the harness.

The probe is a small mod loaded into the instance under test. It drives the
scenario, samples timing, and writes newline-delimited JSON to a file. This
module defines that format and parses it back into the sample types the metric
layer consumes.

Design constraints, all of which follow from not perturbing the thing being
measured:

- **Newline-delimited JSON, append-only.** A crashed or killed run still leaves
  a readable prefix, which is what makes a partial run diagnosable rather than
  lost.
- **Nanosecond integers, never pre-aggregated.** The probe reports raw frame and
  tick durations; every statistic is computed here. If the probe averaged
  anything, a methodology change would require redeploying a Java mod, and old
  results could never be re-analysed under a new rule.
- **Written to a file, not a socket.** No network stack, no blocking writes on
  the render thread, and nothing for a firewall to interfere with.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..metrics import ClientSamples, RunFlag, ServerSamples

__all__ = [
    "PROTOCOL_VERSION",
    "EventType",
    "Phase",
    "ProbeError",
    "ProbeStream",
    "parse_probe_stream",
]

#: Bumped whenever the wire format changes incompatibly. The harness refuses a
#: stream from a probe it does not understand rather than silently
#: misinterpreting fields.
PROTOCOL_VERSION = 1


class EventType(str, Enum):
    HELLO = "hello"
    """First line. Protocol version, probe version, and instance metadata."""
    PHASE = "phase"
    """Transition between provision, warmup, and measurement."""
    FRAME = "frame"
    """A batch of client frame durations, in nanoseconds."""
    TICK = "tick"
    """A batch of server tick durations, in nanoseconds."""
    GC = "gc"
    """Garbage collection pauses observed, in milliseconds."""
    MEMORY = "memory"
    """A heap sample; `post_gc` marks live-set measurements."""
    CHUNK = "chunk"
    """Cumulative chunk generation and load counters."""
    FINGERPRINT = "fingerprint"
    """Hash over generated world blocks in the measurement region."""
    FLAG = "flag"
    """The probe raising a condition that affects admissibility."""
    ERROR = "error"
    BYE = "bye"
    """Clean shutdown. Its absence means the run did not finish."""


class Phase(str, Enum):
    PROVISION = "provision"
    SETUP = "setup"
    """Scenario setup commands are running. Untimed, and never pooled with warmup:
    setup used to run *inside* warmup and spend its budget."""
    WARMUP = "warmup"
    MEASUREMENT = "measurement"


class TickSource(str, Enum):
    """What a batch of tick durations measures.

    The distinction is load-bearing. ``PERIOD`` is the interval between
    end-of-tick callbacks, which on an unsaturated 20 TPS server sits at 50 ms
    whether the tick cost 5 ms or 30 ms, so it cannot be published as MSPT and
    the harness gives it its own metric names instead.
    """

    BRACKET = "bracket"
    PERIOD = "period"
    PLATFORM = "platform"

    @property
    def measures_execution(self) -> bool:
        return self is not TickSource.PERIOD


class ProbeError(RuntimeError):
    """A probe stream is malformed, truncated, or from an incompatible probe."""


@dataclass
class ProbeStream:
    """Everything parsed out of one run's probe output."""

    protocol_version: int = 0
    probe_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    client: ClientSamples = field(default_factory=ClientSamples)
    server: ServerSamples = field(default_factory=ServerSamples)

    world_fingerprint: str = ""
    flags: list[RunFlag] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    completed: bool = False

    tick_source: TickSource = TickSource.BRACKET
    """What the tick durations measure. Defaults to the honest reading for a
    stream that does not say: an old probe, or one whose ticks never arrived."""
    #: What the run reported about how it reached measurement, from ``bye``.
    summary: dict[str, Any] = field(default_factory=dict)
    #: Individual collections, when the JVM could report them.
    gc_events: list[dict[str, Any]] = field(default_factory=list)
    #: True when GC time arrived only as per-interval totals, which cannot
    #: support a pause percentile.
    gc_aggregate_only: bool = False
    #: True when allocation came from a real counter rather than heap growth.
    real_allocation: bool = False

    #: Frames and ticks are retained per phase so warmup can be verified after
    #: the fact rather than trusted.
    warmup_frames_ns: list[int] = field(default_factory=list)
    warmup_ticks_ns: list[int] = field(default_factory=list)

    @property
    def has_client_data(self) -> bool:
        return bool(self.client.frametimes_ns)

    @property
    def has_server_data(self) -> bool:
        return bool(self.server.tick_durations_ns)


def _read_gc(stream: ProbeStream, event: dict[str, Any]) -> None:
    """Read one GC event, in whichever of the three shapes it arrived.

    ``events`` is the good case: individual collections, each with its own
    duration and its own before/after heap, from which a pause percentile is a
    percentile over pauses. ``aggregate`` is the fallback on a JVM with no
    notification support, and is retained as a total only, because computing a
    percentile from per-interval sums would describe the sampling cadence rather
    than the collector. ``pauses_ms`` is the old shape, kept so streams recorded
    before this distinction existed still parse.
    """
    source = event.get("source")

    if source == "events" or "events" in event:
        for raw in event.get("events", []):
            if not isinstance(raw, dict):
                continue
            entry = {
                "collector": str(raw.get("collector", "")),
                "action": str(raw.get("action", "")),
                "duration_ms": float(raw.get("duration_ms", 0.0)),
                "heap_before_mb": float(raw.get("heap_before_mb", 0.0)),
                "heap_after_mb": float(raw.get("heap_after_mb", 0.0)),
                "stop_the_world": bool(raw.get("stop_the_world", True)),
            }
            stream.gc_events.append(entry)
            if entry["stop_the_world"] and entry["duration_ms"] > 0:
                stream.client.gc_pauses_ms.append(entry["duration_ms"])
                stream.server.gc_pauses_ms.append(entry["duration_ms"])
            if entry["heap_after_mb"] > 0:
                # The live set, read at the collection rather than at whatever
                # point the next sampling interval happened to fall.
                stream.client.heap_post_gc_mb.append(entry["heap_after_mb"])
                stream.server.heap_post_gc_mb.append(entry["heap_after_mb"])
        return

    if source == "aggregate":
        stream.gc_aggregate_only = True
        total = float(event.get("total_pause_ms", 0.0))
        stream.client.gc_total_pause_ms += total
        stream.server.gc_total_pause_ms += total
        return

    # Legacy shape: a list of per-interval totals presented as pauses.
    pauses = [float(v) for v in event.get("pauses_ms", [])]
    if pauses:
        stream.gc_aggregate_only = True
        stream.client.gc_total_pause_ms += sum(pauses)
        stream.server.gc_total_pause_ms += sum(pauses)


def _iter_lines(text: str) -> Iterator[tuple[int, dict[str, Any]]]:
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A torn final line is expected when a run is killed mid-write.
            # Everything before it is still valid, so skip rather than fail.
            continue
        if isinstance(event, dict):
            yield number, event


def parse_probe_stream(source: str | Path, *, text: str | None = None) -> ProbeStream:
    """Parse a probe output file into a :class:`ProbeStream`.

    Tolerates truncation: a run killed mid-write leaves a valid prefix, and the
    resulting stream is returned with ``completed`` False so the caller can flag
    it rather than lose the diagnostic value of a partial run.
    """
    if text is None:
        path = Path(source)
        if not path.exists():
            raise ProbeError(f"probe output not found: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")

    stream = ProbeStream()
    phase = Phase.PROVISION
    saw_hello = False

    for number, event in _iter_lines(text):
        kind = event.get("type")

        if kind == EventType.HELLO.value:
            saw_hello = True
            stream.protocol_version = int(event.get("protocol", 0))
            if stream.protocol_version != PROTOCOL_VERSION:
                raise ProbeError(
                    f"{source}:{number}: probe speaks protocol "
                    f"{stream.protocol_version}, harness speaks {PROTOCOL_VERSION}. "
                    f"Refusing to guess at field meanings."
                )
            stream.probe_version = str(event.get("probe_version", ""))
            stream.metadata = dict(event.get("metadata", {}))

        elif kind == EventType.PHASE.value:
            try:
                phase = Phase(event.get("phase", ""))
            except ValueError:
                raise ProbeError(
                    f"{source}:{number}: unknown phase {event.get('phase')!r}"
                ) from None

        elif kind == EventType.FRAME.value:
            durations = [int(v) for v in event.get("durations_ns", [])]
            if phase is Phase.MEASUREMENT:
                stream.client.frametimes_ns.extend(durations)
            elif phase is Phase.WARMUP:
                stream.warmup_frames_ns.extend(durations)

        elif kind == EventType.TICK.value:
            durations = [int(v) for v in event.get("durations_ns", [])]
            declared = event.get("source")
            if declared is not None:
                try:
                    stream.tick_source = TickSource(declared)
                except ValueError:
                    raise ProbeError(
                        f"{source}:{number}: unknown tick source {declared!r}. "
                        f"Refusing to guess whether these are tick durations or "
                        f"tick periods; the two are different measurements."
                    ) from None
            if phase is Phase.MEASUREMENT:
                stream.server.tick_durations_ns.extend(durations)
            elif phase is Phase.WARMUP:
                stream.warmup_ticks_ns.extend(durations)

        elif kind == EventType.GC.value:
            if phase is Phase.MEASUREMENT:
                _read_gc(stream, event)

        elif kind == EventType.MEMORY.value:
            if phase is Phase.MEASUREMENT:
                heap_mb = float(event.get("heap_mb", 0.0))
                stream.client.heap_samples_mb.append(heap_mb)
                stream.server.heap_samples_mb.append(heap_mb)
                if event.get("post_gc"):
                    stream.client.heap_post_gc_mb.append(heap_mb)
                    stream.server.heap_post_gc_mb.append(heap_mb)
                if event.get("real_allocation"):
                    stream.real_allocation = True
                if (allocated := event.get("allocated_bytes")) is not None:
                    stream.client.alloc_bytes = int(allocated)
                    stream.server.alloc_bytes = int(allocated)
                if (growth := event.get("heap_growth_bytes")) is not None:
                    stream.client.heap_growth_bytes = int(growth)
                    stream.server.heap_growth_bytes = int(growth)

        elif kind == EventType.CHUNK.value:
            if phase is Phase.MEASUREMENT:
                stream.server.chunks_generated = int(event.get("generated", 0))
                stream.server.chunks_loaded = int(event.get("loaded", 0))

        elif kind == EventType.FINGERPRINT.value:
            stream.world_fingerprint = str(event.get("sha256", ""))

        elif kind == EventType.FLAG.value:
            try:
                stream.flags.append(RunFlag(event.get("flag", "")))
            except ValueError:
                # An unknown flag from a newer probe is informational, not fatal;
                # record it as an error note so it still surfaces.
                stream.errors.append(f"unknown flag {event.get('flag')!r}")

        elif kind == EventType.ERROR.value:
            stream.errors.append(str(event.get("message", "")))

        elif kind == EventType.BYE.value:
            stream.completed = True
            duration = event.get("measurement_duration_s")
            if duration is not None:
                stream.client.duration_s = float(duration)
                stream.server.wall_clock_s = float(duration)
            stream.server.saturated = bool(event.get("saturated", False))
            stream.summary = {
                key: event[key]
                for key in (
                    "setup_duration", "warmup_duration", "warmup_gate",
                    "warmup_converged", "compilation_gate", "tick_source",
                    "failed_setup_commands", "failed_workload_commands",
                    "real_allocation", "gc_events",
                )
                if key in event
            }
            if "tick_source" in event:
                try:
                    stream.tick_source = TickSource(event["tick_source"])
                except ValueError:
                    stream.errors.append(
                        f"unknown tick source {event['tick_source']!r} in bye"
                    )
            if event.get("real_allocation"):
                stream.real_allocation = True

    if not saw_hello:
        raise ProbeError(
            f"{source}: no 'hello' event; the probe never started. The instance "
            f"most likely failed before the mod loaded; check the instance log."
        )

    if not stream.completed:
        stream.flags.append(RunFlag.CRASHED)

    return stream


#: What the JVM agent writes, always separate from an adapter's own stream.
#: Two writers appending to one file would interleave into an unparseable mess,
#: and both can be active at once: an adapter driving the workload while the
#: agent supplies frames.
AGENT_STREAM_NAME = "probe-agent.jsonl"


def adopt_agent_frames(primary: ProbeStream, agent: ProbeStream) -> str:
    """Fill in frame timings from a JVM-agent stream, in place.

    The agent (``probe/adapters/probe-agent``) times frames by instrumenting
    LWJGL, so it works on versions and loaders that have no adapter. It is a
    timing source and not a platform: it cannot run commands, so it never
    replaces an adapter, only supplements one.

    The rule is **never merge, only substitute**. When an adapter already
    produced frames, both streams are timing the *same* frames by different
    means; concatenating them would double the sample count and halve every
    confidence interval, manufacturing precision that was never measured. So
    the adapter wins and the agent's frames are discarded.

    Substitution is also refused when the two streams disagree about which
    scenario they measured. Instance directories are rebuilt per run, so this
    should be impossible, but a stale file silently contributing frames from a
    different variant is exactly the failure that would destroy a comparison
    while looking perfectly healthy, so it is checked rather than assumed.

    :returns: a short reason describing what was done, for the run event log.
    """
    expected = primary.metadata.get("scenario_hash")
    found = agent.metadata.get("scenario_hash")
    if expected and found and expected != found:
        return "refused: agent stream is from a different scenario"

    if primary.has_client_data:
        return "ignored: the adapter already supplied frames"

    if not agent.client.frametimes_ns:
        return "ignored: the agent recorded no measurement frames"

    primary.client.frametimes_ns.extend(agent.client.frametimes_ns)
    primary.warmup_frames_ns.extend(agent.warmup_frames_ns)
    # The agent runs its own phase controller off the same probe.properties, so
    # its measurement window is the same length as the adapter's but does not
    # start at the same instant: it begins at premain rather than at mod init.
    # Its duration is therefore the honest one to report for these frames.
    if agent.client.duration_s:
        primary.client.duration_s = agent.client.duration_s
    primary.metadata["frame_source"] = "jvm-agent"

    for flag in agent.flags:
        # CRASHED on the agent means its own stream was truncated, which says
        # nothing about the run the adapter completed; the frames it did write
        # are still real. Every other flag qualifies those frames and travels.
        if flag is not RunFlag.CRASHED and flag not in primary.flags:
            primary.flags.append(flag)

    return f"adopted {len(agent.client.frametimes_ns)} frames from the JVM agent"
