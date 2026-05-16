"""Progress reporting protocol used by long-running operations.

Operations call into ``reporter.step()``, ``reporter.log()``, etc.; the
reporter routes those events to whatever surface is driving the call:

- CLI: ``StreamReporter`` writes ``[..]`` / ``[OK]`` / ``[WARN]`` lines to
  stdout, matching the look of the existing ``info()/ok()/warn()`` helpers
  in ``config.py``.
- Textual: a thin reporter wraps a ``ProgressPanel`` widget and uses
  ``app.call_from_thread`` to push updates from the worker thread.

The reporter interface is intentionally tiny so it's trivial to mock in
tests (see /tmp/m3_test.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class OperationResult:
    """Structured outcome of an operation.

    ``success`` is the headline boolean. ``summary`` is a one-line message
    fit for a result modal title. ``detail`` is multi-line text the UI may
    show in a scrollable region (typically the captured tool output).
    ``data`` is operation-specific extra context (e.g. new topology after
    a switchover).
    """

    success: bool
    summary: str
    detail:  str = ""
    data:    dict = field(default_factory=dict)


class ProgressReporter(Protocol):
    """Tiny event sink used by operations to report progress.

    All methods are safe to call from any thread; concrete reporters are
    responsible for marshalling onto their UI thread if needed.
    """

    def step(self, message: str, *, advance: bool = True) -> None:
        """Mark a new (or re-titled) step. Advances the step counter by 1
        unless ``advance=False`` (re-titling without moving the bar)."""

    def log(self, line: str, *, level: str = "info") -> None:
        """Append a line to the streaming log region.

        ``level`` is one of: "info", "ok", "warn", "error". Reporters may
        style accordingly; the operation does not need to."""

    def set_total(self, total: int) -> None:
        """Tell the reporter how many steps the operation will take."""


class StreamReporter:
    """Stdout reporter for CLI use.

    Re-uses the same ``[..] / [OK] / [WARN] / [ERR]`` markers as the
    config helpers so existing scripts that grep our output keep working.
    """

    def __init__(self, *, prefix: str = "  ") -> None:
        self._prefix = prefix
        self._step    = 0
        self._total   = 0

    def set_total(self, total: int) -> None:
        self._total = total

    def step(self, message: str, *, advance: bool = True) -> None:
        if advance:
            self._step += 1
        marker = (f"[{self._step}/{self._total}]" if self._total
                  else "[..]")
        print(f"{self._prefix}{marker} {message}")

    def log(self, line: str, *, level: str = "info") -> None:
        marker = {
            "info":  "    ",
            "ok":    "[OK]",
            "warn":  "[WARN]",
            "error": "[ERR]",
        }.get(level, "    ")
        print(f"{self._prefix}{marker} {line}")
