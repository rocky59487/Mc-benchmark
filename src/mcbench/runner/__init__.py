"""Headless execution: preflight, the probe protocol, and the run loop."""

from .harness import (
    Harness,
    HarnessError,
    ResolvedVariant,
    RunOutcome,
    outcomes_to_cells,
    resolve_variant_mods,
)
from .plan import PlanError, ProbePlan, compile_plan, write_plan
from .preflight import Check, Preflight, Severity, describe_host, run_preflight
from .protocol import (
    PROTOCOL_VERSION,
    EventType,
    Phase,
    ProbeError,
    ProbeStream,
    parse_probe_stream,
)

__all__ = [
    "Harness", "HarnessError", "ResolvedVariant", "RunOutcome",
    "outcomes_to_cells", "resolve_variant_mods",
    "Check", "Preflight", "Severity", "describe_host", "run_preflight",
    "PlanError", "ProbePlan", "compile_plan", "write_plan",
    "PROTOCOL_VERSION", "EventType", "Phase", "ProbeError", "ProbeStream",
    "parse_probe_stream",
]
