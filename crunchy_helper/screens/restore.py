"""Restore flow (Textual, two-phase).

The user navigates a small sub-flow:

    RestoreBackupListScreen   pick a backup label
        |
        v
    PrepareSessionScreen      ProgressPanel: PVC + S3 restore + extract pod
        |  (delivers a BackupSession to the parent screen)
        v
    RestoreSourceListScreen   pick a database FROM THE BACKUP
        |
        v
    RestoreApplyScreen        confirm target name + run the restore
        |
        v
    ResultModal               outcome

Each screen does one thing, so a failure (e.g. S3 creds missing) drops
the user back at a sensible re-entry point. The BackupSession lives on
the top-level RestoreFlowScreen so the user can restore several
databases from the same backup without re-doing S3 download.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, DataTable, Footer, Header, Input,
                             Label, Static)
from textual.worker import Worker, WorkerState

from ..operations import (Backup, BackupSession, OperationResult,
                          RestoreError, cleanup_session, list_backups,
                          list_databases_in_backup,
                          list_session_artifacts,
                          prepare_backup_session, restore_database)
from ..widgets import ProgressPanel, TextualProgressReporter
from ._dialogs import ConfirmModal, ResultModal
from ._table import cell_name, cell_owner, cell_size, status_line


# ---------------------------------------------------------------------------
# Step 1: pick a backup
# ---------------------------------------------------------------------------

class RestoreBackupListScreen(Screen):
    """First step: list pgbackrest backups and let the user pick one.

    Not a list-screen-clone of DatabasesScreen because the data shape is
    different and the action (Enter to proceed) is single-purpose.
    """

    DEFAULT_CSS = """
    RestoreBackupListScreen { padding: 0; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("enter",  "pick",            "Use selected", priority=True),
        Binding("f",      "pick_fresh",      "Fresh from S3"),
        Binding("D",      "cleanup",         "Cleanup PVC"),
        Binding("r",      "refresh",         "Refresh"),
        Binding("escape", "app.pop_screen",  "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sub_title = "Restore - pick a backup"
        self._backups: list[Backup] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(classes="list-screen"):
            yield Static("Restore - step 1 of 3: pick a backup",
                         classes="list-title")
            yield Static(
                "Select a backup. Enter = use (reuses local PVC if one "
                "exists from a previous restore of this date). f = wipe "
                "the local PVC and re-download from S3. Shift+D = delete "
                "the PVC + extract pod for the highlighted date.",
                classes="list-hint")
            yield DataTable(id="bk-table", cursor_type="row",
                            zebra_stripes=True)
            yield Static("Loading...", id="bk-status",
                         classes="list-status")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("LABEL",     width=44, key="label")
        table.add_column("TYPE",      width=8,  key="type")
        table.add_column("WHEN",      width=22, key="when")
        table.add_column("SIZE",      width=12, key="size")
        self.run_worker(self._load(), exclusive=True)

    def action_refresh(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def action_pick(self) -> None:
        self._pick(force_fresh=False)

    def action_pick_fresh(self) -> None:
        self._pick(force_fresh=True)

    def _pick(self, *, force_fresh: bool) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            self.notify("No backup selected.", severity="warning")
            return
        idx = table.cursor_row
        if not (0 <= idx < len(self._backups)):
            return
        backup = self._backups[idx]
        self.run_worker(self._proceed(backup, force_fresh=force_fresh),
                        exclusive=False)

    async def _proceed(self, backup: Backup, *, force_fresh: bool) -> None:
        if force_fresh:
            confirmed = await self.app.push_screen_wait(ConfirmModal(
                title="Re-download from S3",
                message=(f"Wipe the existing PVC for {backup.date} and "
                         f"re-download {backup.label} from S3?\n\n"
                         f"This frees the on-PVC files first, then runs "
                         f"the full ~10-15 minute restore again. Use "
                         f"this if you suspect the local copy is stale."),
                expected=backup.date,
            ))
            if not confirmed:
                return
        session: BackupSession | None = await self.app.push_screen_wait(
            PrepareSessionScreen(backup, force_fresh=force_fresh))
        if session is None:
            return
        await self.app.push_screen_wait(
            RestoreSourceListScreen(session))

    def action_cleanup(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            self.notify("No backup selected.", severity="warning")
            return
        idx = table.cursor_row
        if not (0 <= idx < len(self._backups)):
            return
        backup = self._backups[idx]
        self.run_worker(self._do_cleanup(backup), exclusive=False)

    async def _do_cleanup(self, backup: Backup) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmModal(
            title="Delete restore artifacts",
            message=(f"Delete the PVC and extract pod for {backup.date}?\n"
                     f"\n"
                     f"  - PVC restore-pvc-{backup.date}  (~20Gi)\n"
                     f"  - Pod restore-extract-{backup.date}\n\n"
                     f"This frees disk on the cluster but means the "
                     f"next restore from this date re-downloads from S3."),
            expected=backup.date,
        ))
        if not confirmed:
            return

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _cleanup_blocking, self.app.cfg, backup.date)
        await self.app.push_screen_wait(ResultModal(
            title=result.summary,
            body=result.detail or "(no extra detail)",
            ok=result.success,
        ))

    async def _load(self) -> None:
        status = self.query_one("#bk-status", Static)
        table  = self.query_one(DataTable)
        status.set_classes("list-status")
        status.update("Loading backups from pgbackrest...")
        loop = asyncio.get_running_loop()
        try:
            backups = await loop.run_in_executor(
                None, _list_backups_blocking, self.app.cfg)
        except RestoreError as e:
            status.set_classes("list-status -error")
            status.update(f"{e}  (press r to retry)")
            return
        except Exception as e:
            status.set_classes("list-status -error")
            status.update(f"Unexpected: {type(e).__name__}: {e}  (press r)")
            return
        self._backups = backups
        table.clear()
        for b in backups:
            type_t = Text(b.type, style={
                "FULL": "bold green",
                "DIFF": "yellow",
                "INCR": "cyan",
            }.get(b.type, ""))
            size_t = Text(f"{b.size_mb:.1f} MB", style="cyan")
            size_t.justify = "right"
            table.add_row(
                cell_name(b.label),
                type_t,
                Text(b.timestamp, style="dim"),
                size_t,
                key=b.label,
            )
        # Backups arrive newest-first from list_backups, so the cursor's
        # default position (row 0) is already the most recent. Explicit
        # for clarity in case anyone reorders later.
        if table.row_count > 0:
            table.move_cursor(row=0)
        status.set_classes("list-status -ok")
        status.update(status_line(
            len(backups), "backup",
            "Enter=use", "f=fresh", "Shift+D=cleanup",
            "r=refresh", "Esc=back"))


class _NullReporter:
    """No-op ProgressReporter used when a screen has no ProgressPanel."""
    def set_total(self, n: int) -> None: pass
    def step(self, msg: str, *, advance: bool = True) -> None: pass
    def log(self, line: str, *, level: str = "info") -> None: pass


def _list_backups_blocking(cfg: dict) -> list[Backup]:
    return list_backups(cfg, reporter=_NullReporter())


def _cleanup_blocking(cfg: dict, backup_date: str) -> OperationResult:
    return cleanup_session(cfg, backup_date=backup_date,
                           reporter=_NullReporter())


# ---------------------------------------------------------------------------
# Step 2: prepare backup session (PVC + S3 restore + extract pod)
# ---------------------------------------------------------------------------

class PrepareSessionScreen(ModalScreen[Optional[BackupSession]]):
    """Long-running prep with a ProgressPanel; returns BackupSession."""

    DEFAULT_CSS = """
    PrepareSessionScreen { align: center middle; }
    #prep-card {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #prep-buttons {
        height: 3;
        align: center middle;
        padding-top: 1;
        dock: bottom;
    }
    #prep-buttons Button { margin: 0 1; }
    #prep-hint {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("enter",  "close",  "Continue", priority=True),
        Binding("c",      "close",  "Continue", priority=True, show=False),
        Binding("escape", "cancel", "Cancel",   priority=True),
    ]

    def __init__(self, backup: Backup, *,
                 force_fresh: bool = False) -> None:
        super().__init__()
        self._backup       = backup
        self._force_fresh  = force_fresh
        self._session: Optional[BackupSession] = None
        self._failed:  Optional[RestoreError]  = None
        self._done = False

    def compose(self) -> ComposeResult:
        title_suffix = " (fresh)" if self._force_fresh else ""
        with Container(id="prep-card"):
            yield ProgressPanel(
                f"Preparing restore environment - "
                f"{self._backup.label}{title_suffix}",
                log_min_height=8,
            )
            yield Static("", id="prep-hint")
            with Horizontal(id="prep-buttons"):
                yield Button("Continue", id="prep-ok",
                             variant="primary", disabled=True)
                yield Button("Cancel",   id="prep-cancel")

    def on_mount(self) -> None:
        self.run_worker(self._run, thread=True, exclusive=True,
                        name="prepare", exit_on_error=False)

    def _run(self) -> Optional[BackupSession]:
        # Catch the exception in-thread so the worker reaches SUCCESS
        # state with a None result; on_worker_state_changed reads
        # ``self._failed`` to render the error UI. Letting the exception
        # escape would also make Textual's run_test re-raise it after
        # the screen closes (see Worker.exit_on_error).
        panel = self.query_one(ProgressPanel)
        reporter = TextualProgressReporter(self.app, panel)
        try:
            return prepare_backup_session(
                self.app.cfg, backup=self._backup, reporter=reporter,
                force_fresh=self._force_fresh)
        except RestoreError as e:
            self._failed = e
            return None
        except Exception as e:
            self._failed = RestoreError(
                f"{type(e).__name__}: {e}", "")
            return None

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "prepare":
            return
        if event.state != WorkerState.SUCCESS:
            return
        panel = self.query_one(ProgressPanel)
        ok_btn = self.query_one("#prep-ok", Button)
        hint   = self.query_one("#prep-hint", Static)
        result = event.worker.result
        self._done = True
        if self._failed is not None or result is None:
            err = self._failed or RestoreError("Preparation failed.")
            panel.log(str(err), level="error")
            if err.detail:
                for line in err.detail.splitlines()[-20:]:
                    panel.log(line, level="info")
            panel.finish(success=False, summary=str(err))
            ok_btn.label   = "Close"
            ok_btn.variant = "default"
            ok_btn.disabled = False
            self.query_one("#prep-cancel", Button).disabled = True
            hint.update("Press Enter or click Close to dismiss")
            ok_btn.focus()
            return
        self._session = result
        panel.finish(success=True, summary="Backup environment ready.")
        ok_btn.disabled = False
        self.query_one("#prep-cancel", Button).disabled = True
        hint.update("Press Enter (or click Continue) to pick a database")
        ok_btn.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prep-ok":
            self.action_close()
        else:
            self.action_cancel()

    def action_close(self) -> None:
        # Don't dismiss while still preparing -- the worker is on a
        # background thread and a partial dismiss would hide its result.
        if not self._done:
            self.app.bell()
            return
        self.dismiss(self._session)

    def action_cancel(self) -> None:
        # Esc while running = ignored (we'd lose the worker handle).
        # Esc after success = same as Continue (dismiss with session).
        # Esc after failure = dismiss with None.
        if not self._done:
            self.app.bell()
            return
        self.dismiss(self._session)


# ---------------------------------------------------------------------------
# Step 3: pick a source DB FROM THE BACKUP
# ---------------------------------------------------------------------------

class RestoreSourceListScreen(Screen):
    """List the databases that exist *inside the backup* (not the live
    cluster) and let the user pick one to restore."""

    DEFAULT_CSS = """
    RestoreSourceListScreen { padding: 0; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("enter",  "pick",           "Restore selected",
                priority=True),
        Binding("r",      "refresh",        "Refresh"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, session: BackupSession) -> None:
        super().__init__()
        self._session = session
        self._rows:   list[dict] = []
        self.sub_title = f"Restore - {session.backup.label}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(classes="list-screen"):
            yield Static(
                f"Restore - step 2 of 3: pick a source database",
                classes="list-title")
            yield Static(
                "These databases live INSIDE the backup. Choose one to "
                "restore as a new database on the live cluster.",
                classes="list-hint")
            yield DataTable(id="src-table", cursor_type="row",
                            zebra_stripes=True)
            yield Static("Loading...", id="src-status",
                         classes="list-status")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("DATABASE", width=32, key="name")
        table.add_column("OWNER",    width=20, key="owner")
        table.add_column("SIZE",     width=14, key="size")
        self.run_worker(self._load(), exclusive=True)

    def action_refresh(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    def action_pick(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            self.notify("No database selected.", severity="warning")
            return
        idx = table.cursor_row
        if not (0 <= idx < len(self._rows)):
            return
        row = self._rows[idx]
        if row["name"] in ("postgres", "template0", "template1"):
            self.notify(f"'{row['name']}' is a system database — pick a "
                        "user database instead.", severity="warning")
            return
        self.run_worker(self._proceed(row["name"]), exclusive=False)

    async def _proceed(self, source_db: str) -> None:
        await self.app.push_screen_wait(
            RestoreApplyScreen(self._session, source_db))

    async def _load(self) -> None:
        status = self.query_one("#src-status", Static)
        table  = self.query_one(DataTable)
        status.set_classes("list-status")
        status.update("Listing databases inside the backup...")
        loop = asyncio.get_running_loop()
        try:
            rows = await loop.run_in_executor(
                None, _list_dbs_blocking, self._session)
        except RestoreError as e:
            status.set_classes("list-status -error")
            status.update(f"{e}  (press r to retry)")
            return
        except Exception as e:
            status.set_classes("list-status -error")
            status.update(f"Unexpected: {type(e).__name__}: {e}  (press r)")
            return
        self._rows = rows
        table.clear()
        for r in rows:
            is_system = r["name"] in (
                "postgres", "template0", "template1")
            table.add_row(
                cell_name(r["name"], system=is_system),
                cell_owner(r["owner"]),
                cell_size(r["size"]),
                key=r["name"],
            )
        status.set_classes("list-status -ok")
        status.update(status_line(
            len(rows), "database",
            "Enter=restore", "r=refresh", "Esc=back"))


def _list_dbs_blocking(session: BackupSession) -> list[dict]:
    return list_databases_in_backup(session, reporter=_NullReporter())


# ---------------------------------------------------------------------------
# Step 4: confirm target name and run
# ---------------------------------------------------------------------------

class RestoreApplyScreen(ModalScreen[None]):
    """Form: target database name. On submit, runs restore_database with
    a ProgressPanel and shows the result."""

    DEFAULT_CSS = """
    RestoreApplyScreen { align: center middle; }
    #apply-card {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #apply-form { height: 3; }
    #apply-form Label { width: 18; padding-top: 1; }
    #apply-form Input { width: 1fr; }
    #apply-buttons { height: 3; align: center middle; padding-top: 1; }
    #apply-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, session: BackupSession, source_db: str) -> None:
        super().__init__()
        self._session   = session
        self._source_db = source_db
        # NB: ``_running`` is reserved by Textual (Screen.is_running);
        # use a different name for our "operation in flight" flag.
        self._busy = False

    def compose(self) -> ComposeResult:
        from datetime import date
        default_target = f"{self._source_db}-restored-{date.today().isoformat()}"
        with Container(id="apply-card"):
            yield Static(f"Restore '{self._source_db}'",
                         classes="list-title")
            yield Static(
                "We'll create a new database on the live cluster and "
                "restore the dump into it. The original is untouched.",
                classes="list-hint")
            with Horizontal(id="apply-form", classes="field-row"):
                yield Label("Restore as:")
                yield Input(value=default_target, id="apply-target")
            yield ProgressPanel("Idle", log_min_height=8)
            with Horizontal(id="apply-buttons"):
                yield Button("Restore", id="apply-go", variant="primary")
                yield Button("Cancel",  id="apply-cancel")

    def on_mount(self) -> None:
        self.query_one("#apply-target", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply-go":
            self._kickoff()
        else:
            if self._busy:
                self.app.bell()
                return
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "apply-target" and not self._busy:
            self._kickoff()

    def action_cancel(self) -> None:
        if self._busy:
            self.app.bell()
            return
        self.dismiss(None)

    def _kickoff(self) -> None:
        if self._busy:
            return
        target = self.query_one("#apply-target", Input).value.strip()
        if not target:
            self.notify("Target database name is required.",
                        severity="warning")
            return
        self._busy = True
        self.query_one("#apply-go",     Button).disabled = True
        self.query_one("#apply-cancel", Button).disabled = True
        self.query_one("#apply-target", Input).disabled  = True
        # Re-title the panel to reflect the operation about to run.
        panel = self.query_one(ProgressPanel)
        title = panel.query_one("#pp-title", Static)
        title.update(f"Restoring '{self._source_db}' -> '{target}'")
        self._target = target
        self.run_worker(self._run, thread=True, exclusive=True,
                        name="restore-apply", exit_on_error=False)

    def _run(self) -> OperationResult:
        panel = self.query_one(ProgressPanel)
        reporter = TextualProgressReporter(self.app, panel)
        return restore_database(
            session=self._session,
            source_db=self._source_db,
            target_db=self._target,
            reporter=reporter,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "restore-apply":
            return
        panel = self.query_one(ProgressPanel)
        if event.state == WorkerState.SUCCESS:
            result: OperationResult = event.worker.result
            panel.finish(success=result.success, summary=result.summary)
            self._show_result(result)
        elif event.state == WorkerState.ERROR:
            err = event.worker.error
            msg = f"Worker crashed: {type(err).__name__}: {err}"
            panel.log(msg, level="error")
            panel.finish(success=False, summary=msg)
            self._show_result(OperationResult(False, msg))

    def _show_result(self, result: OperationResult) -> None:
        # Push the ResultModal with a callback that closes us once the
        # user dismisses it. Doing this with push_screen + callback (as
        # opposed to push_screen_wait inside another worker coroutine)
        # avoids spawning a second worker on top of the just-finished
        # one, which can race with the Worker.StateChanged event.
        def _after(_=None) -> None:
            self.dismiss(None)
        self.app.push_screen(
            ResultModal(
                title=result.summary,
                body=result.detail or "(no extra detail)",
                ok=result.success,
            ),
            _after,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

class RestoreFlowScreen:
    """Just a namespace; the flow's entry point is the backup-list
    screen, which is what the main menu pushes."""

    @staticmethod
    def entry() -> Screen:
        return RestoreBackupListScreen()
