"""Main menu — root screen of the Textual app.

Four navigation tiles (Databases / Users / Primary / Quit). Each tile is a
plain Button so screen-readers and keyboard nav both work without extra
wiring; arrow keys move between buttons, Enter activates.

Mouse and Tab/Arrow nav both work out of the box because Textual focuses
the next button when arrow keys are pressed inside a Container.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static


class MainMenuScreen(Screen):
    DEFAULT_CSS = """
    MainMenuScreen {
        align: center middle;
    }
    #menu-container {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #menu-title {
        content-align: center middle;
        padding: 0 0 1 0;
        text-style: bold;
    }
    #menu-buttons {
        height: auto;
    }
    #menu-buttons Button {
        width: 100%;
        margin: 0 0 1 0;
    }
    #menu-hint {
        content-align: center middle;
        color: $text-muted;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("d", "open('databases')", "Databases", show=False),
        Binding("u", "open('users')",     "Users",     show=False),
        Binding("p", "open('primary')",   "Primary",   show=False),
        Binding("R", "open('restore')",   "Restore",   show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Container(id="menu-container"):
            yield Static("Choose an area to manage", id="menu-title")
            with Vertical(id="menu-buttons"):
                yield Button("Databases  (d)",    id="btn-databases",
                             variant="primary")
                yield Button("Users  (u)",        id="btn-users")
                yield Button("Primary  (p)",      id="btn-primary")
                yield Button("Restore  (Shift+R)", id="btn-restore")
                yield Button("Quit  (q)",         id="btn-quit",
                             variant="error")
            yield Static("Press ? for keyboard help", id="menu-hint")

    def on_mount(self) -> None:
        self.query_one("#btn-databases", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-databases":
            self.action_open("databases")
        elif bid == "btn-users":
            self.action_open("users")
        elif bid == "btn-primary":
            self.action_open("primary")
        elif bid == "btn-restore":
            self.action_open("restore")
        elif bid == "btn-quit":
            self.app.exit()

    def action_open(self, area: str) -> None:
        # Imported lazily to keep the screens package import-cycle-free
        # and so optional screens can be added without touching this file.
        if area == "databases":
            from .databases import DatabasesScreen
            self.app.push_screen(DatabasesScreen())
        elif area == "users":
            from .users import UsersScreen
            self.app.push_screen(UsersScreen())
        elif area == "primary":
            from .primary import PrimaryScreen
            self.app.push_screen(PrimaryScreen())
        elif area == "restore":
            from .restore import RestoreFlowScreen
            self.app.push_screen(RestoreFlowScreen.entry())
