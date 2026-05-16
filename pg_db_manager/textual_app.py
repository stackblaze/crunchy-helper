"""Textual-based interactive UI for pg-db-manager.

This is the entry point that `manager.py` calls when invoked with no
arguments. The CLI subcommands (`manager.py list`, `manager.py restore ...`)
do NOT go through here — they keep their plain-stdout behaviour so they're
still usable in scripts, cron jobs, and CI.

Architecture
------------
- ``run_app(cfg)`` is the only public entry point. It constructs the
  ``ManagerApp`` with the loaded config and runs it.
- Screens live under ``pg_db_manager.screens.*``. Each screen owns its own
  layout + key bindings. Cross-screen navigation goes via ``app.push_screen``
  / ``app.pop_screen``.
- Long-running ops (restore phases, switchover, etc.) run in worker threads
  via Textual's ``@work(thread=True)`` so the UI stays responsive. They
  communicate progress back via custom messages.

The current scaffold (M0) is intentionally tiny: it just shows the cluster
name + a "WIP" hint so we can verify the bootstrap and screen routing wire
end-to-end before we start building real screens.
"""

from __future__ import annotations

import traceback

from textual.app import App
from textual.binding import Binding


class ManagerApp(App):
    """Top-level Textual app. Holds the cfg dict and delegates to screens."""

    CSS_PATH = "textual_app.tcss"
    TITLE = "pg-db-manager"
    SUB_TITLE = ""
    BINDINGS = [
        Binding("q",             "request_quit", "Quit",       priority=True),
        Binding("ctrl+c",        "request_quit", "Quit",       show=False,
                priority=True),
        Binding("question_mark", "show_help",    "Help",       priority=True),
        Binding("f1",            "show_help",    "Help",       show=False,
                priority=True),
    ]

    def __init__(self, cfg: dict) -> None:
        super().__init__()
        self.cfg = cfg
        cluster = cfg.get("cluster", "?")
        host    = cfg.get("pg_host", "?")
        ns      = cfg.get("namespace", "?")
        # SUB_TITLE is the class-level default; the live property is
        # `sub_title` (set after super().__init__).
        self.sub_title = f"{cluster}  ·  {ns}  ·  {host}"

    def on_mount(self) -> None:
        # Lazy import keeps the screen tree decoupled from app construction
        # (handy for tests that just want to instantiate ManagerApp).
        from .screens import MainMenuScreen
        self.push_screen(MainMenuScreen())

    # ------------------------------------------------------------------ help
    def action_show_help(self) -> None:
        from .screens._dialogs import HelpModal, collect_bindings
        self.push_screen(HelpModal(collect_bindings(self)))

    # ------------------------------------------------------------------ quit
    def action_request_quit(self) -> None:
        """Quit, but ask first if any worker is currently running.

        Keeps users from accidentally killing a long-running restore /
        switchover by typing 'q' out of habit."""
        active = [w for w in self.workers if w.is_running]
        if not active:
            self.exit()
            return
        names = ", ".join(sorted({w.name or "worker" for w in active}))
        from .screens._dialogs import ConfirmModal

        def _after(confirmed: bool | None) -> None:
            if confirmed:
                self.exit()

        self.push_screen(
            ConfirmModal(
                title="Quit while operation is running?",
                message=(f"There is at least one running operation:\n"
                         f"  {names}\n\n"
                         f"Quitting now will leave its Kubernetes resources "
                         f"in whatever state they're in. Type QUIT to "
                         f"force-quit anyway."),
                expected="QUIT",
            ),
            _after,
        )

    # ------------------------------------------------------------------ errors
    def _handle_exception(self, error: Exception) -> None:
        """Override Textual's bug-catcher to show a polite modal.

        Without this, an uncaught exception in any screen / worker drops
        the user back at a blank terminal with a stack trace. With it,
        they see what went wrong and can dismiss the modal to continue
        using the rest of the app.
        """
        tb = "".join(traceback.format_exception(
            type(error), error, error.__traceback__))[-2000:]
        try:
            from .screens._dialogs import ErrorModal
            self.push_screen(ErrorModal(
                type(error).__name__, str(error) or "(no message)", tb))
        except Exception:
            # If even the modal can't be pushed (e.g. exception during
            # mount), fall back to Textual's default behaviour.
            super()._handle_exception(error)


def run_app(cfg: dict) -> int:
    """Run the Textual app and return its exit code."""
    app = ManagerApp(cfg)
    app.run()
    # Textual sets ``return_code`` to None on a clean exit; normalise to 0.
    return app.return_code or 0
