"""The probe protocol — the contract between the in-game mod and the harness.

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
    WARMUP = "warmup"
    MEASUREMENT = "measurement"


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
            if phase is Phase.MEASUREMENT:
                stream.server.tick_durations_ns.extend(durations)
            elif phase is Phase.WARMUP:
                stream.warmup_ticks_ns.extend(durations)

        elif kind == EventType.GC.value:
            if phase is Phase.MEASUREMENT:
                pauses = [float(v) for v in event.get("pauses_ms", [])]
                stream.client.gc_pauses_ms.extend(pauses)
                stream.server.gc_pauses_ms.extend(pauses)

        elif kind == EventType.MEMORY.value:
            if phase is Phase.MEASUREMENT:
                heap_mb = float(event.get("heap_mb", 0.0))
                stream.client.heap_samples_mb.append(heap_mb)
                stream.server.heap_samples_mb.append(heap_mb)
                if event.get("post_gc"):
                    stream.client.heap_post_gc_mb.append(heap_mb)
                    stream.server.heap_post_gc_mb.append(heap_mb)
                if (allocated := event.get("allocated_bytes")) is not None:
                    stream.client.alloc_bytes = int(allocated)
                    stream.server.alloc_bytes = int(allocated)

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

    if not saw_hello:
        raise ProbeError(
            f"{source}: no 'hello' event — the probe never started. The instance "
            f"most likely failed before the mod loaded; check the instance log."
        )

    if not stream.completed:
        stream.flags.append(RunFlag.CRASHED)

    return stream
