"""Databases screen.

DataTable of databases on the cluster, plus mutating actions:

  c   create database (form)
  d   delete selected database (type-to-confirm)
  i   show connection info for selected database
  r   refresh
  Esc back

Loads via ``fetch_databases`` in a thread executor so the UI doesn't block
on a slow ``kubectl exec``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ..data import DataFetchError, fetch_databases
from ..db_commands import cmd_delete, get_connection_info
from ._dialogs import ConfirmModal, ResultModal
from ._runner import run_captured
from ._table import cell_name, cell_owner, cell_size, status_line


class DatabasesScreen(Screen):
    DEFAULT_CSS = """
    DatabasesScreen { padding: 0; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("c",      "create",   "Create"),
        Binding("d",      "delete",   "Delete"),
        Binding("i",      "info",     "Info"),
        Binding("r",      "refresh",  "Refresh"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sub_title = "Databases"
        self._rows: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(classes="list-screen"):
            yield Static("Databases", classes="list-title")
            yield Static("Logical databases on this PostgreSQL cluster.",
                         classes="list-hint")
            yield DataTable(id="db-table", cursor_type="row",
                            zebra_stripes=True)
            yield Static("Loading...", id="db-status",
                         classes="list-status")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        # Explicit widths keep columns from jumping when zebra stripes
        # repaint or when content varies wildly between refreshes.
        table.add_column("DATABASE",  width=32, key="name")
        table.add_column("OWNER",     width=20, key="owner")
        table.add_column("SIZE",      width=14, key="size")
        self.run_worker(self._load(), exclusive=True)

    def action_refresh(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def action_create(self) -> None:
        from .db_create import CreateDatabaseScreen
        self.run_worker(self._do_create(CreateDatabaseScreen()), exclusive=False)

    def action_delete(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("No database selected.", severity="warning")
            return
        self.run_worker(self._do_delete(row), exclusive=False)

    def action_info(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("No database selected.", severity="warning")
            return
        from .db_info import ConnectionInfoModal
        loop = asyncio.get_running_loop()

        async def show() -> None:
            info = await loop.run_in_executor(
                None, get_connection_info,
                self.app.cfg, row["name"], row["owner"])
            await self.app.push_screen_wait(ConnectionInfoModal(info))

        self.run_worker(show(), exclusive=False)

    def _selected_row(self) -> Optional[dict]:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        idx = table.cursor_row
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    async def _do_create(self, screen) -> None:
        success = await self.app.push_screen_wait(screen)
        if success:
            await self._load()

    async def _do_delete(self, row: dict) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmModal(
            title="Delete database",
            message=(f"This will permanently DROP database '{row['name']}'\n"
                     f"(owner {row['owner']}, size {row['size']}).\n\n"
                     f"All data will be lost."),
            expected=row["name"],
        ))
        if not confirmed:
            return
        args = SimpleNamespace(db=row["name"], yes=True)
        loop = asyncio.get_running_loop()
        success, output = await loop.run_in_executor(
            None, run_captured, cmd_delete, self.app.cfg, args)
        await self.app.push_screen_wait(ResultModal(
            title=(f"Database '{row['name']}' deleted" if success
                   else f"Delete failed: {row['name']}"),
            body=output, ok=success,
        ))
        if success:
            await self._load()

    async def _load(self) -> None:
        status = self.query_one("#db-status", Static)
        table  = self.query_one(DataTable)
        status.set_classes("list-status")
        status.update("Loading...")
        loop = asyncio.get_running_loop()
        try:
            rows = await loop.run_in_executor(
                None, fetch_databases, self.app.cfg)
        except DataFetchError as e:
            status.set_classes("list-status -error")
            status.update(f"Error: {e}  (press r to retry)")
            return
        except Exception as e:
            status.set_classes("list-status -error")
            status.update(f"Unexpected: {type(e).__name__}: {e}  (press r)")
            return
        self._rows = rows
        table.clear()
        admin = self.app.cfg.get("admin_user", "")
        for r in rows:
            is_system = r["name"] == "postgres"
            table.add_row(
                cell_name(r["name"], system=is_system),
                cell_owner(r["owner"], system_admin=admin),
                cell_size(r["size"]),
                key=r["name"],
            )
        n = len(rows)
        status.set_classes("list-status -ok")
        status.update(status_line(
            n, "database",
            "c=create", "d=delete", "i=info", "r=refresh", "Esc=back",
        ))
