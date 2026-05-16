"""Switchover modal: ProgressPanel driving ``perform_switchover``.

Lifecycle:
  1. Compose: ProgressPanel + Close button (disabled until done).
  2. on_mount: spawn a thread worker that runs ``perform_switchover`` with
     a TextualProgressReporter wired to the panel.
  3. Worker finishes -> ``finish()`` paints the result + enables Close.
  4. User presses Esc/Enter -> dismiss with the OperationResult.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from ..operations import OperationResult, perform_switchover
from ..widgets import ProgressPanel, TextualProgressReporter


class SwitchoverScreen(ModalScreen[OperationResult]):
    DEFAULT_CSS = """
    SwitchoverScreen { align: center middle; }
    #sw-card {
        width: 90%;
        max-width: 110;
        height: 80%;
        max-height: 28;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #sw-buttons {
        height: 3;
        align: center middle;
        padding-top: 1;
    }
    #sw-buttons Button { margin: 0 1; }
    #sw-buttons Button:disabled { opacity: 0.4; }
    """

    BINDINGS = [
        Binding("escape", "close",  "Close", priority=True),
        Binding("enter",  "close",  "OK",    priority=True),
    ]

    def __init__(self, *, current_leader: str, target_pod: str) -> None:
        super().__init__()
        self._current_leader = current_leader
        self._target_pod     = target_pod
        self._result: OperationResult | None = None

    def compose(self) -> ComposeResult:
        with Container(id="sw-card"):
            yield ProgressPanel(
                f"Switching primary  {self._current_leader} → {self._target_pod}",
                log_min_height=10,
            )
            with Horizontal(id="sw-buttons"):
                yield Button("Close", id="sw-close", variant="primary",
                             disabled=True)

    def on_mount(self) -> None:
        # Run the operation in a real OS thread (kubectl exec is blocking
        # subprocess work, not asyncio-friendly).
        self.run_worker(self._run_op, thread=True, exclusive=True,
                        name="switchover")

    def _run_op(self) -> OperationResult:
        panel = self.query_one(ProgressPanel)
        reporter = TextualProgressReporter(self.app, panel)
        return perform_switchover(self.app.cfg,
                                  target_pod=self._target_pod,
                                  reporter=reporter)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "switchover":
            return
        if event.state == WorkerState.SUCCESS:
            result = event.worker.result
            self._result = result
            panel = self.query_one(ProgressPanel)
            panel.finish(success=result.success, summary=result.summary)
            close = self.query_one("#sw-close", Button)
            close.disabled = False
            close.focus()
        elif event.state == WorkerState.ERROR:
            err = event.worker.error
            msg = f"Worker crashed: {type(err).__name__}: {err}"
            self._result = OperationResult(False, msg)
            panel = self.query_one(ProgressPanel)
            panel.log(msg, level="error")
            panel.finish(success=False, summary=msg)
            close = self.query_one("#sw-close", Button)
            close.disabled = False
            close.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sw-close":
            self.action_close()

    def action_close(self) -> None:
        # Don't allow Esc/Enter to dismiss while the operation is still
        # running -- a half-done switchover should never be hidden from
        # the operator until they've at least seen the result.
        close = self.query_one("#sw-close", Button)
        if close.disabled:
            self.app.bell()
            return
        self.dismiss(self._result)
