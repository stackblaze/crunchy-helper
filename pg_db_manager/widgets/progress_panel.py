"""ProgressPanel: title + step counter + ProgressBar + streaming RichLog.

Used by every multi-step operation (switchover, restore phases, etc.) so
the user gets the same shape of feedback no matter what's running:

  ┌─ Title (bold, color reflects state) ────────────────────┐
  │ Step 3 of 7  —  Triggering Patroni switchover           │
  │ [████████████████████░░░░░░░░░░░░░░░░░░░░░] 43%          │
  │                                                         │
  │ [..] target = ...        (RichLog)                       │
  │ [OK] exec via ...                                        │
  │ [..] current leader = ...                                │
  └─────────────────────────────────────────────────────────┘

The companion ``TextualProgressReporter`` is what an operation thread
calls into. It marshals updates back onto the Textual event loop using
``app.call_from_thread``, so operations can stay completely UI-unaware.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, ProgressBar, RichLog, Static


class ProgressPanel(Vertical):
    """Vertical container holding all the bits of a progress display."""

    DEFAULT_CSS = """
    ProgressPanel {
        height: auto;
        padding: 0;
    }
    ProgressPanel > #pp-title {
        text-style: bold;
        padding: 0 1;
    }
    ProgressPanel > #pp-title.-running { color: $accent;  }
    ProgressPanel > #pp-title.-ok      { color: $success; }
    ProgressPanel > #pp-title.-error   { color: $error;   }
    ProgressPanel > #pp-title.-warn    { color: $warning; }
    ProgressPanel > #pp-step {
        padding: 0 1;
        color: $text-muted;
    }
    ProgressPanel > ProgressBar {
        padding: 0 1;
    }
    ProgressPanel > RichLog {
        height: 1fr;
        min-height: 8;
        padding: 0 1;
        background: $boost;
        border: tall $background;
    }
    """

    def __init__(self, title: str, *, log_min_height: int = 8) -> None:
        super().__init__()
        self._initial_title = title
        self._log_min       = log_min_height
        self._step          = 0
        self._total         = 0

    def compose(self) -> ComposeResult:
        yield Static(self._initial_title, id="pp-title")
        yield Static("Starting...", id="pp-step")
        yield ProgressBar(total=1, show_eta=False, show_percentage=True,
                          id="pp-bar")
        log = RichLog(id="pp-log", highlight=True, markup=True, wrap=True)
        log.styles.min_height = self._log_min
        yield log

    def on_mount(self) -> None:
        self.query_one("#pp-title", Static).set_classes("-running")

    # --- API used by operations (via the reporter; safe to call from any thread
    #     because the reporter wraps these calls in app.call_from_thread).

    def set_total(self, total: int) -> None:
        self._total = max(1, total)
        self.query_one(ProgressBar).update(total=self._total, progress=0)

    def next_step(self, message: str, *, advance: bool = True) -> None:
        if advance:
            self._step += 1
        bar  = self.query_one(ProgressBar)
        step = self.query_one("#pp-step", Static)
        if self._total:
            step.update(f"Step {self._step} of {self._total}  —  {message}")
        else:
            step.update(message)
        bar.update(progress=min(self._step, self._total or self._step))

    def log(self, line: str, *, level: str = "info") -> None:
        marker = {
            "info":  "[dim][..][/dim]",
            "ok":    "[green][OK][/green]",
            "warn":  "[yellow][WARN][/yellow]",
            "error": "[red][ERR][/red]",
        }.get(level, "[dim][..][/dim]")
        self.query_one(RichLog).write(f"{marker} {line}")

    def finish(self, *, success: bool, summary: str) -> None:
        title = self.query_one("#pp-title", Static)
        title.set_classes("-ok" if success else "-error")
        title.update(("✓ " if success else "✗ ") + summary)
        # Snap the bar to full so the eye gets a clean "done" signal.
        bar = self.query_one(ProgressBar)
        bar.update(progress=self._total or 1)
        step = self.query_one("#pp-step", Static)
        step.update("Done." if success else "Failed.")


class TextualProgressReporter:
    """Adapts a ProgressPanel into a thread-safe ProgressReporter.

    Operations are run on a worker thread and call ``reporter.step()`` /
    ``reporter.log()`` synchronously. Each method bounces its work onto
    the Textual event loop via ``app.call_from_thread`` so widget updates
    happen in the main thread (which is what Textual requires).
    """

    def __init__(self, app, panel: ProgressPanel) -> None:
        self._app   = app
        self._panel = panel

    def set_total(self, total: int) -> None:
        self._app.call_from_thread(self._panel.set_total, total)

    def step(self, message: str, *, advance: bool = True) -> None:
        self._app.call_from_thread(
            self._panel.next_step, message, advance=advance)

    def log(self, line: str, *, level: str = "info") -> None:
        self._app.call_from_thread(self._panel.log, line, level=level)
