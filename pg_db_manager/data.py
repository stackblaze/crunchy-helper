"""Pure data fetchers shared by the Textual screens.

These run plain SQL via ``run_sql_super`` (or kubectl JSON for pod data) and
return lightly-typed dicts. They do NO presentation, NO prompting, NO
sys.exit; on failure they raise ``DataFetchError`` so the calling screen
can decide how to surface it (banner, modal, retry, etc.).

The CLI commands in db_commands.py / user_commands.py / primary_commands.py
keep their own inline SQL because they also drive interactive prompts and
print() banners — those are intentionally separate code paths from the
Textual app.
"""

from __future__ import annotations

import json
from typing import Optional

from .kube import kube, run_sql_super_kubectl


class DataFetchError(RuntimeError):
    """Raised when a fetcher can't produce its result.

    `message` is safe to show in the UI; `detail` (optional) holds the
    raw stderr/stdout for a "show details" disclosure.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


def _safe_run_sql(cfg: dict, db: str, sql: str, *,
                  what: str) -> tuple[str, int]:
    """Wrapper that converts SystemExit / unexpected exceptions raised by
    legacy ``die()`` calls (or other lower-level surprises) into a normal
    ``DataFetchError``.

    The TUI runs fetchers in a worker thread; without this guard a stray
    ``sys.exit(1)`` deep in ``kube.py`` aborts the entire app instead of
    landing on the screen as a recoverable error banner.
    """
    try:
        return run_sql_super_kubectl(cfg, db, sql)
    except SystemExit as e:
        raise DataFetchError(
            f"Could not {what} (legacy helper called sys.exit).",
            f"exit code: {e.code}",
        ) from None
    except Exception as e:
        raise DataFetchError(
            f"Could not {what}: {type(e).__name__}: {e}", "",
        ) from None


def fetch_databases(cfg: dict) -> list[dict]:
    """Return [{name, owner, size}, ...] for non-template databases."""
    out, code = _safe_run_sql(
        cfg, "postgres",
        "SELECT datname, pg_get_userbyid(datdba), "
        "pg_size_pretty(pg_database_size(datname)) "
        "FROM pg_database WHERE datistemplate = false ORDER BY datname;",
        what="query databases",
    )
    if code != 0 or "error:" in out.lower():
        raise DataFetchError("Could not query databases.", out)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            rows.append({"name": parts[0], "owner": parts[1], "size": parts[2]})
    return rows


def fetch_users(cfg: dict, *, exclude_system: bool = True) -> list[dict]:
    """Return [{name, role}, ...] for login roles.

    If exclude_system is true, hide ``postgres`` and the cluster admin from
    the result (those are not safe to delete/edit).
    """
    out, code = _safe_run_sql(
        cfg, "postgres",
        "SELECT rolname, CASE WHEN rolsuper THEN 'superuser' "
        "WHEN rolcreatedb AND rolcreaterole THEN 'createdb+createrole' "
        "WHEN rolcreatedb THEN 'createdb' "
        "WHEN rolcreaterole THEN 'createrole' "
        "ELSE 'normal' END "
        "FROM pg_roles WHERE rolcanlogin = true ORDER BY rolname;",
        what="query users",
    )
    if code != 0:
        raise DataFetchError("Could not query users.", out)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            rows.append({"name": parts[0], "role": parts[1]})
    if exclude_system:
        admin = cfg.get("admin_user", "")
        rows = [r for r in rows if r["name"] not in ("postgres", admin)]
    return rows


def fetch_postgres_pods(cfg: dict) -> list[dict]:
    """Return [{name, node, role, instance_set, ready}, ...] for PG pods.

    `role` is the PGO label value: "master" for the leader, "replica" for
    everyone else. The label can lag the actual Patroni state by a few
    seconds during a switchover; callers that care should cross-reference
    with patronictl (see ``current_patroni_leader``).
    """
    try:
        r = kube("-n", cfg["namespace"], "get", "pod",
                 "-l", f"postgres-operator.crunchydata.com/cluster={cfg['cluster']},"
                       "postgres-operator.crunchydata.com/data=postgres",
                 "-o", "json")
    except Exception as e:
        raise DataFetchError(
            f"kubectl unavailable: {type(e).__name__}: {e}") from None
    if r.returncode != 0:
        raise DataFetchError(
            "Could not list Postgres pods.",
            (r.stderr or b"").decode().strip(),
        )
    try:
        items = json.loads(r.stdout).get("items", [])
    except json.JSONDecodeError as e:
        raise DataFetchError(
            "kubectl returned non-JSON output for pods.", str(e)) from None
    pods = []
    for item in items:
        labels = item["metadata"].get("labels", {})
        ready = all(c.get("ready") for c
                    in item.get("status", {}).get("containerStatuses",
                                                  []) or [{"ready": False}])
        pods.append({
            "name":         item["metadata"]["name"],
            "node":         item["spec"].get("nodeName", "?"),
            "role":         labels.get(
                                "postgres-operator.crunchydata.com/role",
                                "replica"),
            "instance_set": labels.get(
                                "postgres-operator.crunchydata.com/instance-set",
                                ""),
            "ready":        ready,
        })
    pods.sort(key=lambda p: p["name"])
    return pods


def current_patroni_leader(cfg: dict, pods: list[dict]) -> Optional[str]:
    """Return the pod name Patroni currently considers the leader, or None.

    PGO's role label can lag the real Patroni state by 1-5s during a
    switchover. Asking patronictl directly gives us ground truth. We exec
    into any ready non-leader pod (safer: the leader can briefly 502
    during a transition).
    """
    exec_pod = next((p["name"] for p in pods
                     if p["ready"] and p["role"] != "master"), None)
    if exec_pod is None:
        exec_pod = next((p["name"] for p in pods if p["ready"]), None)
    if exec_pod is None:
        return None
    r = kube("-n", cfg["namespace"], "exec", exec_pod, "-c", "database",
             "--", "patronictl", "list", "-f", "json")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        for m in json.loads(r.stdout):
            if m.get("Role", "").lower() == "leader":
                return m.get("Member")
    except json.JSONDecodeError:
        return None
    return None
