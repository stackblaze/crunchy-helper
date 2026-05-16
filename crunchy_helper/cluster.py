"""PostgresCluster spec helpers (get, patch, apply)."""

import json
import subprocess

from .config import die, info
from .kube import kube


def get_cluster_spec(cfg: dict) -> dict:
    r = kube("-n", cfg["namespace"], "get", "postgrescluster", cfg["cluster"], "-o", "json")
    if r.returncode != 0:
        die(f"Failed to get PostgresCluster: {r.stderr.decode().strip()}")
    return json.loads(r.stdout)


def _apply_cluster_spec(spec: dict) -> bool:
    """Replace the live PostgresCluster spec. Falls back to apply if replace fails."""
    payload = json.dumps(spec).encode()
    r = subprocess.run(["kubectl", "replace", "-f", "-"], input=payload, capture_output=True)
    if r.returncode == 0:
        return True
    r = subprocess.run(["kubectl", "apply", "-f", "-"], input=payload, capture_output=True)
    if r.returncode == 0:
        info("(replace not supported on this cluster, used apply)")
        return True
    return False


def patch_spec_remove_db(cfg: dict, db_name: str) -> bool:
    """Remove db_name from all users in the live cluster spec (no-op if not present)."""
    spec = get_cluster_spec(cfg)
    admin = cfg["admin_user"]
    users = spec.get("spec", {}).get("users", [])
    new_users = []
    for u in users:
        u["databases"] = [d for d in u.get("databases", []) if d != db_name]
        if u["name"] == admin or len(u["databases"]) > 0:
            new_users.append(u)
    spec["spec"]["users"] = new_users
    spec.pop("status", None)
    return _apply_cluster_spec(spec)
