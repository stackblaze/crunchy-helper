"""User action modals: create user, reset password.

Both wrap the existing ``cmd_users`` dispatcher with captured output. The
``cmd_users`` CLI uses input() / getpass() when its args don't supply a
value, so we always supply every field to avoid blocking the UI on a
non-existent stdin.
"""

from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from ..data import DataFetchError, fetch_databases
from ..user_commands import cmd_users
from ._dialogs import ResultModal
from ._runner import run_captured


class CreateUserScreen(ModalScreen[bool]):
    """Form: username + grant-on-database + password (auto-gen if blank)."""

    DEFAULT_CSS = """
    CreateUserScreen { align: center middle; }
    #create-card {
        width: 70; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    #create-title { text-style: bold; padding-bottom: 1; }
    #create-hint  { color: $text-muted; padding: 0 0 1 0; }
    .field-row { height: 3; }
    .field-row Label { width: 14; padding-top: 1; }
    .field-row Input { width: 1fr; }
    .field-row Select { width: 1fr; }
    #create-buttons { height: 3; align: center middle; padding-top: 1; }
    #create-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, *, preselected_db: str | None = None) -> None:
        super().__init__()
        self._preselected_db = preselected_db

    def compose(self) -> ComposeResult:
        with Container(id="create-card"):
            yield Static("Create user", id="create-title")
            yield Static("Grants full access on the chosen database. "
                         "Leave password blank to auto-generate.",
                         id="create-hint")
            with Horizontal(classes="field-row"):
                yield Label("Username:")
                yield Input(placeholder="e.g. reporting", id="f-user")
            with Horizontal(classes="field-row"):
                yield Label("Database:")
                # Built empty; populated by _load_dbs() in on_mount once the
                # fetcher returns. Keeping the Select disabled until then
                # prevents an empty-options click.
                yield Select(
                    [("Loading databases...", "")],
                    prompt="Loading databases...",
                    allow_blank=False,
                    id="f-db",
                    disabled=True,
                )
            with Horizontal(classes="field-row"):
                yield Label("Password:")
                yield Input(placeholder="(blank = auto)",
                            password=True, id="f-pw")
            with Horizontal(id="create-buttons"):
                yield Button("Create", id="b-create", variant="primary")
                yield Button("Cancel", id="b-cancel")

    def on_mount(self) -> None:
        self.query_one("#f-user", Input).focus()
        self.run_worker(self._load_dbs(), exclusive=True)

    async def _load_dbs(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            rows = await loop.run_in_executor(
                None, fetch_databases, self.app.cfg)
        except DataFetchError as e:
            self.notify(f"Could not list databases: {e}",
                        severity="error", timeout=8)
            return
        except Exception as e:
            self.notify(f"Unexpected: {type(e).__name__}: {e}",
                        severity="error", timeout=8)
            return
        # Skip postgres (the maintenance DB) — granting on it is almost
        # always a mistake; users can still type it via CLI if they really
        # need to.
        names = [r["name"] for r in rows if r["name"] != "postgres"]
        sel = self.query_one("#f-db", Select)
        if not names:
            sel.set_options([("(no databases found)", "")])
            sel.prompt = "no databases"
            return
        sel.set_options([(n, n) for n in names])
        sel.prompt = "select a database"
        sel.disabled = False
        if self._preselected_db and self._preselected_db in names:
            sel.value = self._preselected_db
        else:
            sel.value = names[0]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "b-create":
            self.run_worker(self._submit(), exclusive=True)
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.run_worker(self._submit(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _submit(self) -> None:
        user = self.query_one("#f-user", Input).value.strip()
        db_v = self.query_one("#f-db",   Select).value
        db   = "" if db_v in (Select.BLANK, None) else str(db_v).strip()
        pw   = self.query_one("#f-pw",   Input).value.strip()
        if not user:
            self.app.bell()
            self.notify("Username is required.", severity="warning")
            return
        if not db:
            self.app.bell()
            self.notify("Pick a database to grant access on.",
                        severity="warning")
            return
        if not pw:
            pw = secrets.token_urlsafe(15)
        args = SimpleNamespace(users_cmd="create",
                               user=user, db=db, password=pw)
        loop = asyncio.get_running_loop()
        success, output = await loop.run_in_executor(
            None, run_captured, cmd_users, self.app.cfg, args)
        await self.app.push_screen_wait(ResultModal(
            title=(f"User '{user}' created" if success
                   else f"Create failed: {user}"),
            body=output, ok=success,
        ))
        self.dismiss(success)


class ResetPasswordScreen(ModalScreen[bool]):
    """Reset password for a pre-selected user."""

    DEFAULT_CSS = """
    ResetPasswordScreen { align: center middle; }
    #reset-card {
        width: 70; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    #reset-title { text-style: bold; padding-bottom: 1; }
    #reset-hint  { color: $text-muted; padding-bottom: 1; }
    .field-row { height: 3; }
    .field-row Label { width: 14; padding-top: 1; }
    .field-row Input { width: 1fr; }
    #reset-buttons { height: 3; align: center middle; padding-top: 1; }
    #reset-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, username: str) -> None:
        super().__init__()
        self._username = username

    def compose(self) -> ComposeResult:
        with Container(id="reset-card"):
            yield Static(f"Reset password — {self._username}",
                         id="reset-title")
            yield Static("Leave blank to auto-generate.", id="reset-hint")
            with Horizontal(classes="field-row"):
                yield Label("New password:")
                yield Input(placeholder="(blank = auto)",
                            password=True, id="f-pw")
            with Horizontal(id="reset-buttons"):
                yield Button("Reset", id="b-reset", variant="primary")
                yield Button("Cancel", id="b-cancel")

    def on_mount(self) -> None:
        self.query_one("#f-pw", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "b-reset":
            self.run_worker(self._submit(), exclusive=True)
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.run_worker(self._submit(), exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def _submit(self) -> None:
        pw = self.query_one("#f-pw", Input).value.strip()
        if not pw:
            pw = secrets.token_urlsafe(15)
        args = SimpleNamespace(users_cmd="reset-password",
                               user=self._username, password=pw)
        loop = asyncio.get_running_loop()
        success, output = await loop.run_in_executor(
            None, run_captured, cmd_users, self.app.cfg, args)
        await self.app.push_screen_wait(ResultModal(
            title=(f"Password reset for '{self._username}'" if success
                   else f"Reset failed: {self._username}"),
            body=output, ok=success,
        ))
        self.dismiss(success)
