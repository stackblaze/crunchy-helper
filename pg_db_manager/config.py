"""Configuration and output helpers for pg-db-manager."""

import getpass
import os
import re
import sys
from pathlib import Path

# Project root (parent of this package) for pg-db-manager.env and YAML paths
SCRIPT_DIR = Path(__file__).resolve().parent.parent
DIVIDER = "═" * 48


def load_env():
    """Source pg-db-manager.env into os.environ (handles `export KEY=VALUE`).
    Paths in .env can use ${SCRIPT_DIR}; relative KUBECONFIG/YAML are resolved from project root for portability.
    """
    env_var = os.environ.get("PG_DB_MANAGER_ENV")
    candidates = (
        [Path(env_var)] if env_var
        else [SCRIPT_DIR / "pg-db-manager.env", Path.home() / ".pg-db-manager.env"]
    )
    for path in candidates:
        if path.exists():
            _parse_env_file(path)
            _resolve_relative_paths()
            return


def _resolve_relative_paths():
    """Resolve relative KUBECONFIG from project root so the same .env works when the project is copied."""
    val = os.environ.get("KUBECONFIG")
    if val and not Path(val).is_absolute():
        os.environ["KUBECONFIG"] = str((SCRIPT_DIR / val).resolve())


def _parse_env_file(path: Path):
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        val = val.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
        val = val.replace("${SCRIPT_DIR}", str(SCRIPT_DIR)).replace("$SCRIPT_DIR", str(SCRIPT_DIR))
        os.environ.setdefault(key, val)


def get_config() -> dict:
    required = ["PG_HOST", "PG_HOST_IP", "NAMESPACE", "CLUSTER"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        ask_reconfigure_then_die(f"Missing required config: {', '.join(missing)}")
    return {
        "pg_host":    os.environ["PG_HOST"],
        "pg_host_ip": os.environ["PG_HOST_IP"],
        "pg_port":    os.environ.get("PG_PORT", "5432"),
        "namespace":  os.environ["NAMESPACE"],
        "cluster":    os.environ["CLUSTER"],
        "admin_user": os.environ.get("PGADMIN_USER", "pgadmin"),
    }


def die(msg: str, code: int = 1):
    print(f"\n  [ERROR] {msg}\n", file=sys.stderr)
    sys.exit(code)


def ask_reconfigure_then_die(msg: str, code: int = 1):
    """Print error, offer to run setup wizard; if accepted, reconfigure and exit 0."""
    print(f"\n  [ERROR] {msg}\n", file=sys.stderr)
    msg_lower = msg.lower()
    if "name resolution" in msg_lower or "could not translate host name" in msg_lower:
        pg_host = os.environ.get("PG_HOST", "")
        print("  PG_HOST is set to an in-cluster DNS name that can't be resolved from this host.", file=sys.stderr)
        if pg_host:
            print(f"  Current PG_HOST: {pg_host}", file=sys.stderr)
        print("  Set PG_HOST to a reachable external IP or hostname (LoadBalancer, NodePort, etc.).", file=sys.stderr)
        print("  Then reconfigure below to save the new value.\n", file=sys.stderr)
    try:
        reply = input("  Reconfigure profile? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        reply = ""
    if reply in ("y", "yes"):
        from .setup import run_setup_wizard
        run_setup_wizard()
        print("  Run your command again.\n")
        sys.exit(0)
    sys.exit(code)


def ok(msg: str):
    print(f"  [OK] {msg}")


def info(msg: str):
    print(f"  [..] {msg}")


def warn(msg: str):
    print(f"  [WARN] {msg}")


def divider():
    print(DIVIDER)


def require(value, prompt: str, secret: bool = False) -> str:
    """Return value if truthy, otherwise prompt the user interactively."""
    if value:
        return value
    if secret:
        return getpass.getpass(f"  {prompt}: ")
    return input(f"  {prompt}: ").strip()
