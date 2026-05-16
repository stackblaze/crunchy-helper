"""Setup wizard: generates pg-db-manager.env when config is missing. Called automatically by manager."""

import base64
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .config import SCRIPT_DIR, die, divider, info, ok, warn
from . import kube as kube_module


def _decode(b):
    """Decode subprocess bytes to str for display and comparison."""
    if b is None:
        return ""
    return b.decode() if isinstance(b, bytes) else str(b)


def _ask(prompt: str, default: str = "") -> str:
    if default:
        reply = input(f"  {prompt} [{default}]: ").strip()
        return reply if reply else default
    return input(f"  {prompt}: ").strip()


def _detect_from_kubeconfig() -> tuple:
    """
    Read kubeconfig and query cluster to get all we can without prompting.
    Returns (kubeconfig_path, server, clusters_list) where clusters_list is [(namespace, cluster), ...].
    """
    default_kube = os.environ.get("KUBECONFIG", str(Path.home() / ".kube" / "config"))
    kubeconfig_path = default_kube.replace("~", str(Path.home()))
    if not Path(kubeconfig_path).is_file():
        return "", "", []

    os.environ["KUBECONFIG"] = kubeconfig_path
    r = kube_module.kube("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    server = _decode(r.stdout).strip() if r.returncode == 0 else ""

    r = kube_module.kube("get", "postgrescluster", "-A", "-o", "custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name", "--no-headers")
    if r.returncode != 0:
        return kubeconfig_path, server, []
    clusters = []
    for line in _decode(r.stdout).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            clusters.append((parts[0].strip(), parts[1].strip()))
    return kubeconfig_path, server, clusters


def _step1_kubeconfig() -> str:
    print("  Step 1/4  Kubeconfig")
    print()
    default_kube = os.environ.get("KUBECONFIG", str(Path.home() / ".kube" / "config"))
    kubeconfig_path = _ask("Path to kubeconfig", default_kube)
    kubeconfig_path = kubeconfig_path.replace("~", str(Path.home()))
    if not Path(kubeconfig_path).is_file():
        die(f"File not found: {kubeconfig_path}")
    os.environ["KUBECONFIG"] = kubeconfig_path
    ok(f"Kubeconfig: {kubeconfig_path}")
    print()
    return kubeconfig_path


def _step2_api_server(kubeconfig_path: str) -> str:
    print("  Step 2/4  Kubernetes API server")
    print()
    r = kube_module.kube("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    current_server = _decode(r.stdout).strip() if r.returncode == 0 else ""
    print(f"  Current server : {current_server or '(unknown)'}")
    print("  Enter an external URL to override (e.g. https://pg-miami.example.com:6443).")
    print("  Press Enter to keep the current server.")
    print()
    ext_server = _ask("External API server URL", current_server)
    if ext_server and ext_server != current_server:
        patched = SCRIPT_DIR / ".kubeconfig-patched.yaml"
        shutil.copy2(kubeconfig_path, patched)
        patched.chmod(0o600)
        with open(patched) as f:
            cfg = yaml.safe_load(f)
        for c in cfg.get("clusters", []):
            c["cluster"]["server"] = ext_server
        with open(patched, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        os.environ["KUBECONFIG"] = str(patched)
        ok(f"Server patched → {ext_server}")
        kubeconfig_path = str(patched)
    else:
        ok(f"Keeping server: {current_server}")
    print()
    print("  Testing kubectl connectivity...", end="", flush=True)
    r = kube_module.kube("get", "namespaces", "-o", "name")
    if r.returncode != 0:
        print(" FAILED")
        die(f"Cannot reach the cluster.\n  Details: {_decode(r.stderr).strip()}")
    print(" OK")
    print()
    return kubeconfig_path


def _step3_namespace_cluster() -> tuple:
    print("  Step 3/4  Namespace & PostgresCluster")
    print()
    kubeconfig_path, _server, clusters = _detect_from_kubeconfig()
    if not clusters:
        r = kube_module.kube("get", "namespaces", "-o", "custom-columns=NAME:.metadata.name", "--no-headers")
        print("  Available namespaces:")
        for line in _decode(r.stdout).splitlines():
            print(f"    {line.strip()}")
        print()
        namespace = _ask("Namespace", "postgres")
        print()
        r = kube_module.kube("-n", namespace, "get", "postgrescluster", "-o", "custom-columns=NAME:.metadata.name", "--no-headers")
        cluster_list = [l.strip() for l in _decode(r.stdout).splitlines() if l.strip()]
        print(f"  PostgresClusters in '{namespace}':")
        if not cluster_list:
            die("No PostgresClusters found. Verify namespace and that PGO is installed.")
        for name in cluster_list:
            print(f"    {name}")
        print()
        cluster = _ask("Cluster name", cluster_list[0] if len(cluster_list) == 1 else "")
        if not cluster:
            die("Cluster name cannot be empty.")
        ok(f"Namespace: {namespace}   Cluster: {cluster}")
        print()
        return namespace, cluster

    if len(clusters) == 1:
        namespace, cluster = clusters[0]
        ok(f"Using sole PostgresCluster: namespace={namespace}   cluster={cluster}")
        print()
        return namespace, cluster

    print("  PostgresClusters found:")
    for i, (ns, name) in enumerate(clusters):
        print(f"    {i + 1}) {name} (namespace: {ns})")
    print()
    # Prefer cluster in namespace 'postgres' (typical workload ns) over e.g. postgres-operator
    default_cluster = clusters[0][1]
    for ns, name in clusters:
        if ns == "postgres":
            default_cluster = name
            break
    default = f"{default_cluster}"
    choice = _ask("Cluster name (or number)", default)
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(clusters):
            namespace, cluster = clusters[idx]
            ok(f"Namespace: {namespace}   Cluster: {cluster}")
            print()
            return namespace, cluster
    for ns, name in clusters:
        if name == choice:
            ok(f"Namespace: {ns}   Cluster: {name}")
            print()
            return ns, name
    namespace, cluster = clusters[0]
    ok(f"Namespace: {namespace}   Cluster: {cluster}")
    print()
    return namespace, cluster


def _get_cluster_spec_users(namespace: str, cluster: str) -> list:
    """Return list of user names from PostgresCluster spec."""
    r = kube_module.kube("-n", namespace, "get", "postgrescluster", cluster, "-o", "json")
    if r.returncode != 0:
        return []
    try:
        doc = json.loads(r.stdout)
        users = doc.get("spec", {}).get("users", [])
        return [u.get("name") for u in users if u.get("name")]
    except (json.JSONDecodeError, KeyError):
        return []


def _detect_cluster_ip(namespace: str, cluster: str) -> str:
    """Return ClusterIP of the HA service (non-headless), empty string if not found."""
    r = kube_module.kube("-n", namespace, "get", "svc", f"{cluster}-ha",
                         "-o", "jsonpath={.spec.clusterIP}")
    ip = _decode(r.stdout).strip()
    return ip if ip and ip != "None" else ""


def _detect_external_ips(namespace: str, cluster: str) -> list:
    """Return external IPs of the HA service when type=LoadBalancer.

    K3S Klipper ServiceLB exposes a LoadBalancer service on every node IP, so
    this typically returns one entry per node. Empty list if service is not
    LoadBalancer or no external IPs are assigned yet.
    """
    r = kube_module.kube("-n", namespace, "get", "svc", f"{cluster}-ha",
                         "-o", "jsonpath={.status.loadBalancer.ingress[*].ip}")
    raw = _decode(r.stdout).strip()
    if not raw:
        return []
    return [ip for ip in raw.split() if ip]


def _is_reachable(host: str, port: int = 5432, timeout: float = 2.0) -> bool:
    """Quick TCP check — True if host:port accepts a connection."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _step4_pg_connection(namespace: str, cluster: str) -> tuple:
    print("  Step 4/4  PostgreSQL connection info")
    print()
    default_host = f"{cluster}-primary.{namespace}.svc.cluster.local"
    pg_host_ip = ""

    # Prefer LoadBalancer external IPs (HA path: any node IP works via Klipper ServiceLB).
    external_ips = _detect_external_ips(namespace, cluster)
    if external_ips:
        ok(f"HA service is LoadBalancer with external IPs: {', '.join(external_ips)}")
        reachable = next((ip for ip in external_ips if _is_reachable(ip)), "")
        if reachable:
            pg_host_ip = reachable
            print(f"  Using {reachable} for PG_HOST_IP (any node IP would also work).")
            print(f"  Recommended PG_HOST: a DNS name with A records for ALL node IPs")
            print(f"  so libpq can fail over (e.g. pg-{cluster}.example.com).")
        else:
            warn("LoadBalancer IPs found but none reachable from this host.")
            pg_host_ip = external_ips[0]

    # Fall back to ClusterIP (in-cluster use only).
    if not pg_host_ip:
        cluster_ip = _detect_cluster_ip(namespace, cluster)
        if cluster_ip and _is_reachable(cluster_ip):
            ok(f"Cluster HA service reachable at {cluster_ip}:5432")
            pg_host_ip = cluster_ip
        else:
            print("  PG_HOST is the hostname or IP used to connect to PostgreSQL.")
            print("  If running outside the cluster, enter an externally reachable address.")
            print("  The in-cluster default only works from inside Kubernetes.")
            pg_host_ip = "127.0.0.1"
    print()
    users = _get_cluster_spec_users(namespace, cluster)
    default_admin = users[0] if users else cluster
    pg_host = _ask("PG_HOST (shown in connection strings)", default_host)
    pg_port = _ask("PG_PORT", "5432")
    admin_user = _ask(f"PGO admin username  (secret: {cluster}-pguser-<name>)", default_admin)
    ok(f"PG host: {pg_host}:{pg_port}   Admin user: {admin_user}")
    print()
    return pg_host, pg_port, pg_host_ip, admin_user


def _write_env(kubeconfig_path: str, pg_host: str, pg_port: str, pg_host_ip: str,
               namespace: str, cluster: str, admin_user: str):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    ENV_OUT = SCRIPT_DIR / "pg-db-manager.env"
    try:
        kube_rel = Path(kubeconfig_path).resolve().relative_to(SCRIPT_DIR)
        kubeconfig_export = f"${{SCRIPT_DIR}}/{kube_rel}"
    except (ValueError, TypeError):
        kubeconfig_export = kubeconfig_path
    content = f"""\
# pg-db-manager — {cluster} (namespace: {namespace})
# Generated by setup on {now}

export KUBECONFIG="{kubeconfig_export}"
PG_HOST="{pg_host}"
PG_PORT="{pg_port}"
PG_HOST_IP="{pg_host_ip}"
NAMESPACE="{namespace}"
CLUSTER="{cluster}"
PGADMIN_USER="{admin_user}"
"""
    ENV_OUT.write_text(content)
    divider()
    ok(f"Written: {ENV_OUT}")
    divider()
    print()


def _verify_admin_secret(namespace: str, cluster: str, admin_user: str):
    secret_name = f"{cluster}-pguser-{admin_user}"
    print(f"  Verifying admin secret '{secret_name}'...")
    print()
    r = kube_module.kube("-n", namespace, "get", "secret", secret_name)
    if r.returncode != 0:
        r2 = kube_module.kube("-n", namespace, "get", "postgrescluster", cluster, "-o", "json")
        spec_users = []
        if r2.returncode == 0:
            try:
                doc = json.loads(r2.stdout)
                spec_users = [u.get("name") for u in doc.get("spec", {}).get("users", []) if u.get("name")]
            except (json.JSONDecodeError, KeyError):
                pass
        if admin_user not in spec_users:
            die(f"Secret '{secret_name}' does not exist. Admin user must be in PostgresCluster spec. Env was written.")
        info("Waiting for PGO to create secret (up to 30s)...")
        for i in range(6):
            time.sleep(5)
            r = kube_module.kube("-n", namespace, "get", "secret", secret_name)
            if r.returncode == 0:
                ok("Secret created")
                break
            info(f"({i + 1}/6)")
        else:
            die("Secret was not created after 30s. Check PGO operator. Env was written.")
    pw = ""
    for attempt in range(24):
        r = kube_module.kube("-n", namespace, "get", "secret", secret_name, "-o", "jsonpath={.data.password}")
        if r.returncode == 0 and r.stdout.strip():
            try:
                pw = base64.b64decode(r.stdout.strip()).decode()
                if pw:
                    break
            except Exception:
                pass
        if attempt == 0:
            r2 = kube_module.kube("-n", namespace, "annotate", "postgrescluster", cluster,
                                  f"postgres-operator.crunchydata.com/trigger-reconcile={int(time.time())}", "--overwrite")
            if r2.returncode == 0:
                ok("Reconcile triggered")
        info(f"Waiting for password... ({attempt + 1}/24)")
        time.sleep(5)
    if not pw:
        die("Admin secret password still empty after waiting. Env was written.")
    ok("Admin secret ready")
    print()


def _check_primary_node(namespace: str, cluster: str):
    """Die if this machine is not the primary PostgreSQL node."""
    import socket
    hostname = socket.gethostname()
    r = kube_module.kube("-n", namespace, "get", "pod",
                         "-l", "postgres-operator.crunchydata.com/role=master",
                         "-o", "jsonpath={.items[0].spec.nodeName}")
    node_name = _decode(r.stdout).strip()
    if not node_name:
        warn("Could not determine primary node — skipping node check.")
        return
    if hostname != node_name:
        die(
            f"This tool must run on the primary PostgreSQL node.\n"
            f"  Primary node : {node_name}\n"
            f"  This machine : {hostname}\n"
            f"\n  SSH to '{node_name}' and run this tool there."
        )
    ok(f"Running on primary node: {hostname}")
    print()


def _verify_psql(namespace: str, cluster: str, admin_user: str) -> bool:
    """Verify DB connectivity via crictl exec (peer auth) on the primary container."""
    print("  Verifying psql connectivity...")
    from . import config
    config.load_env()
    cfg = config.get_config()
    out, code = kube_module.run_sql_super(cfg, "postgres", "SELECT 'crictl-auth-ok';")
    if code != 0 or "error" in out.lower():
        warn(f"psql test failed: {out.strip()}")
        return False
    ok("psql connectivity confirmed")
    return True


def run_setup_wizard():
    """Run interactive setup and write pg-db-manager.env. Uses config.SCRIPT_DIR for paths."""
    print()
    divider()
    print("  pg-db-manager — Environment Setup")
    divider()
    print("  No config found. Generating pg-db-manager.env for your Crunchy PGO cluster.")
    print()
    kubeconfig_path = _step1_kubeconfig()
    kubeconfig_path = _step2_api_server(kubeconfig_path)
    namespace, cluster = _step3_namespace_cluster()
    _check_primary_node(namespace, cluster)
    pg_host, pg_port, pg_host_ip, admin_user = _step4_pg_connection(namespace, cluster)
    _write_env(kubeconfig_path, pg_host, pg_port, pg_host_ip, namespace, cluster, admin_user)
    _verify_admin_secret(namespace, cluster, admin_user)
    psql_ok = _verify_psql(namespace, cluster, admin_user)
    if psql_ok:
        ok("Setup complete.")
    else:
        ok("Setup complete (DB connectivity could not be verified — list/create/delete may still fail).")
    print()


def ensure_configured():
    """Load env; if required config is missing, run setup wizard then reload env."""
    from . import config
    config.load_env()
    required = ["PG_HOST", "PG_HOST_IP", "NAMESPACE", "CLUSTER"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        run_setup_wizard()
        config.load_env()
