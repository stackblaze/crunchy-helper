"""Connection-info modal: read-only display of credentials + URLs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConnectionInfoModal(ModalScreen[None]):
    DEFAULT_CSS = """
    ConnectionInfoModal { align: center middle; }
    #info-card {
        width: 90%;
        max-width: 110;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #info-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #info-body {
        padding-bottom: 1;
    }
    #info-buttons { height: 3; align: center middle; padding-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter",  "dismiss", "OK",    priority=True),
    ]

    def __init__(self, info: dict) -> None:
        super().__init__()
        self._info = info

    def compose(self) -> ComposeResult:
        i = self._info
        body = (
            f"[b]Host[/b]      : {i['host']}\n"
            f"[b]Port[/b]      : {i['port']}\n"
            f"[b]Database[/b]  : {i['database']}\n"
            f"[b]User[/b]      : {i['user']}\n"
            f"[b]Password[/b]  : {i['password']}\n"
            f"\n"
            f"[b]URL[/b]\n  {i['url']}\n"
            f"\n"
            f"[b]JDBC[/b]\n  {i['jdbc']}\n"
        )
        with Container(id="info-card"):
            yield Static(f"Connection info — {i['database']}", id="info-title")
            yield Static(body, id="info-body")
            yield Button("Close", id="info-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
