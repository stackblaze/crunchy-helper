"""Users screen.

DataTable of login roles + actions:

  c   create user (form)
  d   delete selected user (type-to-confirm; system roles can't be deleted)
  p   reset password for selected user
  r   refresh
  Esc back
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

from ..data import DataFetchError, fetch_users
from ..user_commands import cmd_users
from ._dialogs import ConfirmModal, ResultModal
from ._runner import run_captured
from ._table import cell_name, cell_role, status_line


# System roles we never let the user mutate from the TUI. Mirrors the
# guard in cmd_users("delete"). Includes a runtime addition for the
# cluster admin user (read from cfg at action time).
_FROZEN_ROLES = {"postgres"}


class UsersScreen(Screen):
    DEFAULT_CSS = """
    UsersScreen { padding: 0; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("c",      "create",           "Create"),
        Binding("d",      "delete",           "Delete"),
        Binding("p",      "reset_password",   "Reset password"),
        Binding("r",      "refresh",          "Refresh"),
        Binding("escape", "app.pop_screen",   "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sub_title = "Users"
        self._rows: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(classes="list-screen"):
            yield Static("Users", classes="list-title")
            yield Static("Login roles. System roles (postgres + cluster "
                         "admin) are dimmed and cannot be deleted.",
                         classes="list-hint")
            yield DataTable(id="user-table", cursor_type="row",
                            zebra_stripes=True)
            yield Static("Loading...", id="user-status",
                         classes="list-status")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("USERNAME", width=32, key="name")
        table.add_column("ROLE",     width=24, key="role")
        self.run_worker(self._load(), exclusive=True)

    def action_refresh(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def action_create(self) -> None:
        from .user_actions import CreateUserScreen
        self.run_worker(self._do_create(CreateUserScreen()), exclusive=False)

    def action_delete(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("No user selected.", severity="warning")
            return
        admin = self.app.cfg.get("admin_user", "")
        if row["name"] in _FROZEN_ROLES or row["name"] == admin:
            self.notify(f"'{row['name']}' is a system role — cannot delete.",
                        severity="warning")
            return
        self.run_worker(self._do_delete(row), exclusive=False)

    def action_reset_password(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("No user selected.", severity="warning")
            return
        from .user_actions import ResetPasswordScreen
        self.run_worker(
            self._do_reset(ResetPasswordScreen(row["name"])),
            exclusive=False)

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

    async def _do_reset(self, screen) -> None:
        # No table refresh needed — password change isn't visible in the
        # USER/ROLE columns. The result modal already showed the new pw.
        await self.app.push_screen_wait(screen)

    async def _do_delete(self, row: dict) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmModal(
            title="Delete user",
            message=(f"Drop role '{row['name']}' (role: {row['role']}).\n\n"
                     f"Owned objects are reassigned to postgres before drop. "
                     f"The credential secret in Kubernetes is also removed."),
            expected=row["name"],
        ))
        if not confirmed:
            return
        args = SimpleNamespace(users_cmd="delete", user=row["name"], yes=True)
        loop = asyncio.get_running_loop()
        success, output = await loop.run_in_executor(
            None, run_captured, cmd_users, self.app.cfg, args)
        await self.app.push_screen_wait(ResultModal(
            title=(f"User '{row['name']}' deleted" if success
                   else f"Delete failed: {row['name']}"),
            body=output, ok=success,
        ))
        if success:
            await self._load()

    async def _load(self) -> None:
        status = self.query_one("#user-status", Static)
        table  = self.query_one(DataTable)
        status.set_classes("list-status")
        status.update("Loading...")
        loop = asyncio.get_running_loop()
        try:
            rows = await loop.run_in_executor(
                None,
                lambda: fetch_users(self.app.cfg, exclude_system=False),
            )
        except DataFetchError as e:
            status.set_classes("list-status -error")
            status.update(f"Error: {e}  (press r to retry)")
            return
        except Exception as e:
            status.set_classes("list-status -error")
            status.update(f"Unexpected: {type(e).__name__}: {e}  (press r)")
            return
        self._rows = rows
        admin = self.app.cfg.get("admin_user", "")
        frozen = _FROZEN_ROLES | ({admin} if admin else set())
        table.clear()
        for r in rows:
            is_system = r["name"] in frozen
            table.add_row(
                cell_name(r["name"], system=is_system),
                cell_role(r["role"]),
                key=r["name"],
            )
        n = len(rows)
        status.set_classes("list-status -ok")
        status.update(status_line(
            n, "user",
            "c=create", "d=delete", "p=password", "r=refresh", "Esc=back",
        ))
