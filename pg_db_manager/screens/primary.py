"""Primary screen — show Patroni topology and trigger switchover.

Read-only DataTable + an `s` action that opens the SwitchoverScreen for
the currently-highlighted (non-leader) pod.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ..data import DataFetchError, current_patroni_leader, fetch_postgres_pods
from ._dialogs import ConfirmModal
from ._table import (cell_name, cell_node, cell_pod_role, cell_ready,
                     status_line)


class PrimaryScreen(Screen):
    DEFAULT_CSS = """
    PrimaryScreen { padding: 0; }
    #pri-leader {
        padding: 0 2;
        height: 1;
        text-style: bold;
        color: $accent;
    }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("s",      "switch",         "Switch primary"),
        Binding("r",      "refresh",        "Refresh"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sub_title = "Primary / topology"
        self._pods: list[dict] = []
        self._leader: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(classes="list-screen"):
            yield Static("Patroni topology", classes="list-title")
            yield Static("",  id="pri-leader")
            yield Static("Pods labelled with the postgres role; the leader "
                         "row is highlighted. Press s to switch primary.",
                         classes="list-hint")
            yield DataTable(id="pri-table", cursor_type="row",
                            zebra_stripes=True)
            yield Static("Loading...", id="pri-status",
                         classes="list-status")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("POD",   width=42, key="pod")
        table.add_column("NODE",  width=32, key="node")
        table.add_column("ROLE",  width=10, key="role")
        table.add_column("READY", width=8,  key="ready")
        self.run_worker(self._load(), exclusive=True)

    def action_refresh(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def action_switch(self) -> None:
        target = self._selected_pod()
        if target is None:
            self.notify("No pod selected.", severity="warning")
            return
        if not target["ready"]:
            self.notify(f"'{target['name']}' is not Ready — cannot promote.",
                        severity="warning")
            return
        if target["name"] == self._leader:
            self.notify(f"'{target['name']}' is already the primary.",
                        severity="information")
            return
        if not self._leader:
            self.notify("Current leader unknown — refresh first.",
                        severity="warning")
            return
        self.run_worker(self._do_switch(target), exclusive=False)

    def _selected_pod(self) -> Optional[dict]:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        idx = table.cursor_row
        if 0 <= idx < len(self._pods):
            return self._pods[idx]
        return None

    async def _do_switch(self, target: dict) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmModal(
            title="Switchover",
            message=(f"Promote '{target['name']}' (node {target['node']})?\n\n"
                     f"Current primary: {self._leader}\n"
                     f"Patroni will demote it; clients with HA-aware\n"
                     f"connection strings reconnect within seconds."),
            expected=target["name"],
        ))
        if not confirmed:
            return
        from .switchover import SwitchoverScreen
        await self.app.push_screen_wait(SwitchoverScreen(
            current_leader=self._leader,
            target_pod=target["name"],
        ))
        await self._load()

    async def _load(self) -> None:
        status = self.query_one("#pri-status", Static)
        leader = self.query_one("#pri-leader", Static)
        table  = self.query_one(DataTable)
        status.set_classes("list-status")
        status.update("Loading...")
        leader.update("")
        loop = asyncio.get_running_loop()
        try:
            pods = await loop.run_in_executor(
                None, fetch_postgres_pods, self.app.cfg)
            patroni_leader = await loop.run_in_executor(
                None,
                lambda: current_patroni_leader(self.app.cfg, pods),
            )
        except DataFetchError as e:
            status.set_classes("list-status -error")
            status.update(f"Error: {e}  (press r to retry)")
            return
        except Exception as e:
            status.set_classes("list-status -error")
            status.update(f"Unexpected: {type(e).__name__}: {e}  (press r)")
            return

        self._pods   = pods
        self._leader = patroni_leader

        table.clear()
        for p in pods:
            is_leader = p["name"] == patroni_leader
            table.add_row(
                cell_name(p["name"]),
                cell_node(p["node"]),
                cell_pod_role(p.get("role", "replica"), is_leader=is_leader),
                cell_ready(p["ready"]),
                key=p["name"],
            )

        leader.update(
            f"Current primary: "
            f"{patroni_leader or '(unknown - Patroni unreachable)'}"
        )
        n = len(pods)
        status.set_classes("list-status -ok")
        status.update(status_line(
            n, "pod",
            "s=switch", "r=refresh", "Esc=back",
        ))
