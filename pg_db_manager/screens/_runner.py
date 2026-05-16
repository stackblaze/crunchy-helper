"""Run an existing CLI ``cmd_*`` function and capture its output.

The CLI commands print their progress + results to stdout/stderr and call
``sys.exit()`` on failure. To reuse them from the Textual screens (without
killing the app or having two competing implementations), we capture both
streams and intercept SystemExit. The result is a tuple suitable for
display in a ResultModal.

This is the "M2 pragmatic" path. M3+ moves heavier flows (restore,
switchover) to pure-function operations with structured progress events;
the small create/delete/info flows stay on this capture-and-display
pattern because the CLI output is genuinely the most useful diagnostic.
"""

from __future__ import annotations

import contextlib
import io
from typing import Callable


def run_captured(func: Callable, *args, **kwargs) -> tuple[bool, str]:
    """Run ``func(*args, **kwargs)`` with stdout+stderr captured.

    Returns ``(success, combined_output)``.

    - ``success`` is True iff the function returned normally with no
      ``SystemExit(code != 0)``.
    - ``combined_output`` interleaves stdout and stderr in the order they
      were written (good enough for our short CLI ops; we don't need a
      real PTY merge).
    - Unexpected exceptions are caught too — their type + message gets
      appended so the user sees something useful instead of a blank
      success modal.
    """
    buf = io.StringIO()
    success = True
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            func(*args, **kwargs)
    except SystemExit as e:
        # die() calls sys.exit(1); cmd_* commands rely on this for fatal
        # errors. Treat a non-zero/None code as failure.
        code = e.code if isinstance(e.code, int) else 1
        success = (code == 0)
    except Exception as e:
        success = False
        buf.write(f"\n[unexpected] {type(e).__name__}: {e}\n")
    return success, buf.getvalue().strip()
