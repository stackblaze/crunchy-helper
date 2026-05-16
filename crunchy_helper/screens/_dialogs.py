"""Reusable modal dialogs used across the action screens."""

from __future__ import annotations

from typing import Iterable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class ResultModal(ModalScreen[None]):
    """Show captured CLI output in a scrollable region with an OK button.

    Used after running an existing ``cmd_*`` function — the captured stdout
    + stderr is displayed verbatim so the user gets exactly the same
    diagnostics they'd see on the CLI.
    """

    DEFAULT_CSS = """
    ResultModal { align: center middle; }
    #result-card {
        width: 90%;
        max-width: 100;
        height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #result-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #result-title.-error  { color: $error;   }
    #result-title.-ok     { color: $success; }
    #result-body {
        height: 1fr;
        background: $boost;
        padding: 1;
        border: tall $background;
        overflow-y: auto;
    }
    #result-buttons {
        height: 3;
        align: center middle;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter",  "dismiss", "OK",    priority=True),
    ]

    def __init__(self, *, title: str, body: str, ok: bool) -> None:
        super().__init__()
        self._title = title
        self._body  = body
        self._ok    = ok

    def compose(self) -> ComposeResult:
        with Container(id="result-card"):
            t = Static(self._title, id="result-title")
            t.set_classes("-ok" if self._ok else "-error")
            yield t
            yield Static(self._body or "(no output)", id="result-body")
            with Horizontal(id="result-buttons"):
                yield Button("Close", id="result-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Type-to-confirm dialog. User must type ``expected`` exactly to proceed.

    Used for destructive actions (delete database / user / switchover) where
    a single Y/N keystroke is too easy to fat-finger. Mirrors the CLI's
    "Type {name} to confirm:" pattern from cmd_delete().
    """

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #confirm-card {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }
    #confirm-title {
        text-style: bold;
        color: $error;
        padding-bottom: 1;
    }
    #confirm-msg { padding-bottom: 1; }
    #confirm-input { margin-bottom: 1; }
    #confirm-buttons { height: 3; align: center middle; }
    #confirm-buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, *, title: str, message: str, expected: str) -> None:
        super().__init__()
        self._title    = title
        self._message  = message
        self._expected = expected

    def compose(self) -> ComposeResult:
        with Container(id="confirm-card"):
            yield Static(self._title, id="confirm-title")
            yield Static(self._message, id="confirm-msg")
            yield Input(placeholder=f"Type {self._expected!r} to confirm",
                        id="confirm-input")
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", id="confirm-yes", variant="error")
                yield Button("Cancel",  id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-yes":
            self._submit()
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        v = self.query_one("#confirm-input", Input).value.strip()
        if v == self._expected:
            self.dismiss(True)
        else:
            # Don't dismiss; flash the input red and let them retry.
            inp = self.query_one("#confirm-input", Input)
            inp.value = ""
            inp.placeholder = f"doesn't match - type {self._expected!r}"

    def action_cancel(self) -> None:
        self.dismiss(False)


class HelpModal(ModalScreen[None]):
    """Show the active key bindings as a tidy reference.

    Built dynamically from ``app.namespace_bindings`` so we never lie
    about what the keys do (no separate doc string to drift). Pops up
    on `?` from any screen.
    """

    DEFAULT_CSS = """
    HelpModal { align: center middle; }
    #help-card {
        width: 80%;
        max-width: 90;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #help-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #help-body {
        height: auto;
        max-height: 32;
        padding: 0 1;
        background: $boost;
        border: tall $background;
        overflow-y: auto;
    }
    #help-body .row { height: 1; }
    #help-body .key {
        width: 18;
        color: $accent;
        text-style: bold;
    }
    #help-body .desc { width: 1fr; }
    #help-section {
        text-style: bold;
        color: $text-muted;
        padding-top: 1;
    }
    #help-buttons { height: 3; align: center middle; padding-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("question_mark", "dismiss", "Close", priority=True,
                show=False),
        Binding("enter", "dismiss", "Close", priority=True, show=False),
    ]

    def __init__(self,
                 sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
        super().__init__()
        # Sections: [(heading, [(key, description), ...]), ...]
        self._sections = sections

    def compose(self) -> ComposeResult:
        with Container(id="help-card"):
            yield Static("Keyboard reference", id="help-title")
            with VerticalScroll(id="help-body"):
                for heading, rows in self._sections:
                    if heading:
                        yield Static(heading, id="help-section")
                    for key, desc in rows:
                        with Horizontal(classes="row"):
                            yield Static(key,  classes="key")
                            yield Static(desc, classes="desc")
            with Horizontal(id="help-buttons"):
                yield Button("Close", id="help-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


def collect_bindings(app) -> list[tuple[str, list[tuple[str, str]]]]:
    """Build help sections from the app + the focused screen.

    We avoid Textual's deprecated ``namespace_bindings`` and instead read
    BINDINGS off each class explicitly; this works across Textual minor
    versions and gives us full control over labels.
    """
    def _rows_from(cls) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for binding in getattr(cls, "BINDINGS", []) or []:
            # BINDINGS entries can be Binding instances or tuples; we only
            # render Binding instances and skip ones marked ``show=False``
            # (those are usually aliases like Ctrl+C for quit).
            if not isinstance(binding, Binding):
                continue
            if binding.show is False:
                continue
            label = (binding.description
                     or binding.action.replace("_", " ").title())
            rows.append((_pretty_key(binding.key), label))
        # de-dup adjacent identical keys (we sometimes register two
        # bindings to the same action for ergonomics, e.g. enter + space)
        seen, deduped = set(), []
        for k, d in rows:
            sig = (k, d)
            if sig in seen:
                continue
            seen.add(sig)
            deduped.append((k, d))
        return deduped

    sections: list[tuple[str, list[tuple[str, str]]]] = []

    # Per-screen bindings first -- they're the most relevant to the user
    # right now.
    screen = app.screen if app.screen_stack else None
    if screen is not None:
        screen_rows = _rows_from(type(screen))
        if screen_rows:
            sections.append(
                (f"This screen: {type(screen).__name__}", screen_rows))

    # Then the app-level globals.
    app_rows = _rows_from(type(app))
    if app_rows:
        sections.append(("Always available", app_rows))

    if not sections:
        sections.append(("", [("(no bindings)", "")]))
    return sections


def _pretty_key(key: str) -> str:
    """Translate Textual key strings into user-friendly labels.

    e.g. ``ctrl+c`` -> ``Ctrl+C``, ``question_mark`` -> ``?``."""
    aliases = {
        "question_mark":     "?",
        "exclamation_mark":  "!",
        "left_square_bracket":  "[",
        "right_square_bracket": "]",
        "underscore":        "_",
        "minus":             "-",
        "plus":              "+",
        "equals_sign":       "=",
        "full_stop":         ".",
        "comma":             ",",
        "slash":             "/",
        "semicolon":         ";",
        "apostrophe":        "'",
        "grave_accent":      "`",
        "tilde":             "~",
    }
    pieces = key.split("+")
    has_modifier = len(pieces) > 1
    parts = []
    for i, piece in enumerate(pieces):
        if piece in aliases:
            parts.append(aliases[piece])
        elif len(piece) == 1:
            # Uppercase single-char keys when they're paired with a
            # modifier (Ctrl+C reads better than Ctrl+c).
            parts.append(piece.upper() if has_modifier and i > 0 else piece)
        else:
            parts.append(piece.title())
    return "+".join(parts)


class ErrorModal(ModalScreen[None]):
    """Last-resort modal for an unhandled exception inside a screen.

    Shown by ``ManagerApp._handle_exception`` so a failed screen can't
    take the whole TUI down. The user gets the type + message + a
    truncated traceback they can copy out.
    """

    DEFAULT_CSS = """
    ErrorModal { align: center middle; }
    #err-card {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }
    #err-title {
        text-style: bold;
        color: $error;
        padding-bottom: 1;
    }
    #err-body {
        height: auto;
        max-height: 24;
        padding: 0 1;
        background: $boost;
        border: tall $background;
        overflow-y: auto;
    }
    #err-buttons { height: 3; align: center middle; padding-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("enter",  "dismiss", "Close", priority=True),
    ]

    def __init__(self, exc_type: str, exc_msg: str, tb: str) -> None:
        super().__init__()
        self._title = f"Unhandled error: {exc_type}"
        self._body = (f"{exc_msg}\n\n"
                      f"--- traceback (most recent call last) ---\n{tb}")

    def compose(self) -> ComposeResult:
        with Container(id="err-card"):
            yield Static(self._title, id="err-title")
            with VerticalScroll(id="err-body"):
                yield Static(self._body)
            with Horizontal(id="err-buttons"):
                yield Button("Dismiss", id="err-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
