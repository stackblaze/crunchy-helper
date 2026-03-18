"""Kubernetes and PostgreSQL helpers (kubectl, crictl, primary container, secrets)."""

import base64
import json
import os
import subprocess

from .config import ask_reconfigure_then_die, die

# Module-level cache so we only look up the container ID once per run
_primary_container_id: str = ""


def kube(*args, input_data: bytes = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl"] + list(args),
        capture_output=True,
        input=input_data,
    )


def get_primary_pod(cfg: dict) -> str:
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", "postgres-operator.crunchydata.com/role=master",
             "-o", "jsonpath={.items[0].metadata.name}")
    pod = r.stdout.decode().strip()
    if not pod:
        die("Could not find primary pod. Is the cluster running?")
    return pod


def get_primary_container_id(cfg: dict) -> str:
    """Return the crictl container ID for the primary database container on this node."""
    global _primary_container_id
    if _primary_container_id:
        return _primary_container_id

    pod_name = get_primary_pod(cfg)
    r = subprocess.run(["sudo", "crictl", "ps", "-o", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die("crictl unavailable or requires sudo. Ensure this script runs on the primary node.")

    try:
        containers = json.loads(r.stdout).get("containers", [])
    except json.JSONDecodeError:
        die("Could not parse crictl output.")

    for c in containers:
        if (c.get("metadata", {}).get("name") == "database"
                and c.get("labels", {}).get("io.kubernetes.pod.name") == pod_name
                and c.get("state") == "CONTAINER_RUNNING"):
            _primary_container_id = c["id"]
            return _primary_container_id

    die(f"Database container for pod '{pod_name}' not found via crictl. "
        f"Is this the primary node?")


def run_sql_super(cfg: dict, db: str, sql: str) -> tuple:
    """Run SQL as postgres superuser via crictl exec (local peer auth, no password needed)."""
    container_id = get_primary_container_id(cfg)
    r = subprocess.run(
        ["sudo", "crictl", "exec", "-i", container_id,
         "psql", "-d", db, "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )
    return (r.stdout + r.stderr).strip(), r.returncode


def run_sql_direct(cfg: dict, user: str, password: str, db: str, sql: str) -> tuple:
    """Run SQL as a specific user via direct TCP to pg_host_ip:pg_port (SCRAM + SSL)."""
    env = {**os.environ, "PGPASSWORD": password, "PGSSLMODE": "prefer"}
    r = subprocess.run(
        ["psql", "-h", cfg["pg_host_ip"], "-p", str(cfg["pg_port"]),
         "-U", user, "-d", db, "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=30, env=env,
    )
    return (r.stdout + r.stderr).strip(), r.returncode


def get_primary_node(cfg: dict) -> str:
    """Return the node name the primary pod is scheduled on."""
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", "postgres-operator.crunchydata.com/role=master",
             "-o", "jsonpath={.items[0].spec.nodeName}")
    node = r.stdout.decode().strip()
    if not node:
        die("Could not determine primary node name.")
    return node


def get_primary_pod_spec(cfg: dict) -> dict:
    """Return image and pgbackrest-config volume from the primary pod (used by restore)."""
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", "postgres-operator.crunchydata.com/role=master", "-o", "json")
    if r.returncode != 0:
        die("Could not get primary pod.")
    data = json.loads(r.stdout.decode())
    items = data.get("items") or []
    if not items:
        die("No primary pod found.")
    spec = items[0]["spec"]
    image = None
    pgbackrest_volume = None
    for c in spec.get("containers", []):
        if c["name"] == "database":
            image = c["image"]
            break
    for v in spec.get("volumes", []):
        if v["name"] == "pgbackrest-config":
            pgbackrest_volume = v
            break
    if not image or not pgbackrest_volume:
        die("Primary pod missing database image or pgbackrest-config volume.")
    return {"image": image, "pgbackrest_volume": pgbackrest_volume}


def get_secret_password(cfg: dict, secret_name: str) -> str:
    r = kube("-n", cfg["namespace"], "get", "secret", secret_name,
             "-o", "jsonpath={.data.password}")
    if r.returncode != 0:
        return ""
    encoded = r.stdout.decode().strip()
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded).decode()
    except Exception:
        return ""


def check_primary_node(cfg: dict):
    """Die if this machine is not running the primary PostgreSQL pod."""
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", "postgres-operator.crunchydata.com/role=master",
             "-o", "jsonpath={.items[0].spec.nodeName}")
    node_name = r.stdout.decode().strip()
    if not node_name:
        die("Could not determine primary node. Is the cluster running?")
    hostname = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    if hostname != node_name:
        die(
            f"This tool must run on the primary PostgreSQL node.\n"
            f"  Primary node : {node_name}\n"
            f"  This machine : {hostname}\n"
            f"\n  SSH to '{node_name}' and run this tool there."
        )


def preflight(cfg: dict):
    r = kube("get", "namespace", cfg["namespace"], "-o", "name")
    if r.returncode != 0:
        details = (r.stderr or b"").decode().strip()
        kubeconfig = os.environ.get("KUBECONFIG", "~/.kube/config (default)")
        ask_reconfigure_then_die(
            f"kubectl cannot reach the cluster.\n  Details: {details}\n\n"
            f"  KUBECONFIG: {kubeconfig}"
        )
    check_primary_node(cfg)


def reset_container_id_cache():
    global _primary_container_id
    _primary_container_id = ""


def pgbackrest_exec(cfg: dict, container_id: str, cmd: str) -> subprocess.CompletedProcess:
    """Run a pgbackrest command inside the database container via crictl."""
    return subprocess.run(
        ["sudo", "crictl", "exec", "-i", container_id, "bash", "-c", cmd],
        capture_output=True, text=True,
    )
