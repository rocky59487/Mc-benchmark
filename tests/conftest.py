"""Shared test setup.

The one thing here keeps the suite off the machine it is running on.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_process_table(monkeypatch):
    """Answer the harness's competing-process check without asking the OS.

    Every run samples the process table either side of its launch, which on
    Windows means a PowerShell CIM query over every process on the machine.
    Real in a benchmark, where it costs a second between two runs that take
    five minutes each; absurd in a test suite, where it was four fifths of the
    runtime and made the result depend on what else the developer had open.

    Tests that are about this check override it again with their own values.
    """
    monkeypatch.setattr(
        "mcbench.runner.harness.competing_minecraft", lambda: [], raising=False
    )
