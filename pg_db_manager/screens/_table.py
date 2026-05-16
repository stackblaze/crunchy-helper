"""Shared DataTable helpers.

Each list screen builds rich-text cells (so we can color a "leader" row
green or grey out a system role) instead of plain strings. Centralising
the styling here keeps the three screens visually consistent and makes
it trivial to retheme later -- one tweak here propagates everywhere.

We intentionally do NOT use emojis (project rule); status uses short
text indicators ("yes"/"NO", "leader"/"replica") with color instead.
"""

from __future__ import annotations

from rich.text import Text


# ---- Cell builders ------------------------------------------------------

def cell_name(name: str, *, system: bool = False) -> Text:
    """Primary identifier (database name, username, pod name).

    System rows (postgres / cluster admin) are dimmed so they visually
    recede -- they're the rows you typically *can't* mutate."""
    if system:
        return Text(name, style="dim italic")
    return Text(name, style="bold")


def cell_owner(owner: str, *, system_admin: str = "postgres") -> Text:
    if owner == system_admin:
        return Text(owner, style="dim")
    return Text(owner)


def cell_size(size: str) -> Text:
    """Human-readable byte size (e.g. '7574 kB'). Right-aligned, accented
    so it stands out from the descriptive columns."""
    t = Text(size, style="cyan")
    t.justify = "right"
    return t


def cell_role(role: str) -> Text:
    """Postgres role classification.

    Green for normal app roles, red for superuser, yellow for elevated."""
    style = {
        "superuser":            "bold red",
        "createdb+createrole":  "bold yellow",
        "createdb":             "yellow",
        "createrole":           "yellow",
        "normal":               "green",
    }.get(role, "")
    return Text(role, style=style)


def cell_pod_role(role: str, *, is_leader: bool) -> Text:
    """Patroni role for the Primary screen. Leaders get a bright marker;
    replicas are quietly green; downed nodes will show as dimmed via
    the ready cell."""
    if is_leader:
        t = Text("leader", style="bold green")
    else:
        t = Text(role, style="dim")
    return t


def cell_ready(ready: bool) -> Text:
    if ready:
        return Text("yes", style="green")
    return Text("NO", style="bold red blink")


def cell_node(node: str) -> Text:
    """Kubernetes node name -- usually the longest column. Plain text but
    truncates gracefully via DataTable column width."""
    return Text(node, style="dim")


# ---- Status-line builder -----------------------------------------------

def status_line(count: int, noun: str, *bindings: str) -> str:
    """Compose a "<n> users.  c=create  d=delete  ..." footer string.

    Bindings should be passed as already-formatted "key=label" parts; we
    join them with two spaces so they breathe a bit on a wide terminal."""
    plural = "" if count == 1 else "s"
    parts  = [f"{count} {noun}{plural}."] + list(bindings)
    return "  ".join(parts)
