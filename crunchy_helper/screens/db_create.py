"""Create-database modal: small form, runs cmd_create, shows result."""

from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..db_commands import cmd_create
from ._dialogs import ResultModal
from ._runner import run_captured


class CreateDatabaseScreen(ModalScreen[bool]):
    """Form: db name, user, password (auto-generated if blank).

    On submit, ``cmd_create`` runs in an executor so the UI stays responsive
    during the kubectl exec round-trips. Resolves with True if the create
    succeeded so the parent screen can refresh its DataTable.
    """

    DEFAULT_CSS = """
    CreateDatabaseScreen { align: center middle; }
    #create-card {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #create-title { text-style: bold; padding-bottom: 1; }
    .field-row { height: 3; }
    .field-row Label { width: 14; padding-top: 1; }
    .field-row Input { width: 1fr; }
    #create-hint { color: $text-muted; padding: 0 0 1 0; }
    #create-buttons { height: 3; align: center middle; padding-top: 1; }
    #create-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def compose(self) -> ComposeResult:
        with Container(id="create-card"):
            yield Static("Create database", id="create-title")
            yield Static("Leave password blank to auto-generate.",
                         id="create-hint")
            with Horizontal(classes="field-row"):
                yield Label("Database:")
                yield Input(placeholder="e.g. orders", id="f-db")
            with Horizontal(classes="field-row"):
                yield Label("User:")
                yield Input(placeholder="e.g. orders", id="f-user")
            with Horizontal(classes="field-row"):
                yield Label("Password:")
                yield Input(placeholder="(blank = auto)",
                            password=True, id="f-pw")
            with Horizontal(id="create-buttons"):
                yield Button("Create", id="b-create", variant="primary")
                yield Button("Cancel", id="b-cancel")

    def on_mount(self) -> None:
        self.query_one("#f-db", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "b-create":
            self.run_worker(self._submit(), exclusive=True)
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter on any input -> submit the form (mirrors typical form UX
        # and keeps the modal usable from keyboard alone).
        self.run_worker(self._submit(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _submit(self) -> None:
        db   = self.query_one("#f-db",   Input).value.strip()
        user = self.query_one("#f-user", Input).value.strip()
        pw   = self.query_one("#f-pw",   Input).value.strip()
        if not db or not user:
            self.app.bell()
            self.notify("Database and user are required.", severity="warning")
            return
        if not pw:
            pw = secrets.token_urlsafe(15)

        args = SimpleNamespace(db=db, user=user, password=pw)
        loop = asyncio.get_running_loop()
        success, output = await loop.run_in_executor(
            None, run_captured, cmd_create, self.app.cfg, args)

        title = (f"Database '{db}' created" if success
                 else f"Create failed: {db}")
        await self.app.push_screen_wait(
            ResultModal(title=title, body=output, ok=success))
        self.dismiss(success)
