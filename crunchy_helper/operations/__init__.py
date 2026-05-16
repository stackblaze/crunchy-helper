"""Pure-function operations.

These modules hold the business logic that has both a CLI surface (via
the existing ``cmd_*`` functions in ``db_commands.py``, ``user_commands.py``,
``primary_commands.py``, ``restore.py``) and a Textual surface. The
operations themselves are I/O against kubectl / patronictl / SQL with no
``print`` and no ``sys.exit``; they report progress through a
``ProgressReporter`` interface and return a structured result.

This pattern is what makes the Textual ProgressPanel + RichLog possible:
the worker thread drives the operation, the reporter posts updates back
to the UI, and the operation's return value tells the screen whether to
show a success or failure result.
"""

from .progress import OperationResult, ProgressReporter, StreamReporter
from .restore import (Backup, BackupSession, RestoreError, cleanup_session,
                      list_backups, list_databases_in_backup,
                      list_session_artifacts, prepare_backup_session,
                      restore_database)
from .switchover import perform_switchover

__all__ = [
    "Backup",
    "BackupSession",
    "OperationResult",
    "ProgressReporter",
    "RestoreError",
    "StreamReporter",
    "cleanup_session",
    "list_backups",
    "list_databases_in_backup",
    "list_session_artifacts",
    "perform_switchover",
    "prepare_backup_session",
    "restore_database",
]
