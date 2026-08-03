"""What the CLI writes has to survive the console it is written to.

Every check in doctor and validate is reported with a status mark, and none of
the three exist in cp950 — the codepage a Traditional Chinese Windows install
uses when Python's output is a pipe rather than a console. `mcbench validate`
exited with UnicodeEncodeError from the line announcing that everything was
valid, and only when redirected, which is how CI and every wrapper script runs
it.
"""

from __future__ import annotations

import io
import sys

import pytest

from mcbench.cli import main

MARKS = "✓✗⚠"


def _console(encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, line_buffering=True)


class TestStatusMarksReachATerminal:
    @pytest.mark.parametrize("encoding", ["cp950", "cp1252", "ascii", "utf-8"])
    def test_validate_survives_a_console_that_cannot_spell_its_own_output(
        self, encoding, monkeypatch
    ):
        stream = _console(encoding)
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", stream)

        assert main(["validate"]) == 0

        stream.flush()
        written = stream.buffer.getvalue().decode("utf-8")
        assert "scenario(s) valid" in written

    def test_the_marks_themselves_are_not_silently_dropped(self, monkeypatch):
        # errors="replace" would also stop the crash, and would report every
        # check as a question mark.
        stream = _console("cp950")
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", stream)

        main(["validate"])

        stream.flush()
        written = stream.buffer.getvalue().decode("utf-8")
        assert any(mark in written for mark in MARKS)
        assert "?" not in written.replace("? ", "")

    def test_a_stream_that_cannot_be_reconfigured_is_left_alone(self, monkeypatch):
        # StringIO has no reconfigure, and capsys hands one to every other test
        # in the repository.
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())

        assert main(["validate"]) == 0
