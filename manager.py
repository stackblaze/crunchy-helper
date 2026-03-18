#!/usr/bin/env python3
"""pg-db-manager — Manage databases on a Crunchy PGO PostgreSQL cluster.

Usage:
  ./manager.py list [--db NAME]
  ./manager.py create [--db NAME] [--user USER] [--password PASS]
  ./manager.py delete [--db NAME] [--yes]
  ./manager.py restore [--backup LABEL] [--source-db NAME] [--as NAME] [--yes]
  ./manager.py users list
  ./manager.py users create [--user USER] [--db NAME] [--password PASS]
  ./manager.py users delete [--user USER] [--yes]
  ./manager.py users reset-password [--user USER] [--password PASS]

If pg-db-manager.env is missing, setup runs automatically.
"""

import argparse
import shutil
import subprocess
import sys


import os as _os
_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))

# apt packages needed: pip (for Python deps) and psql (for DB connectivity fallback)
_APT_PKGS = {
    "python3-pip":        lambda: subprocess.run([sys.executable, "-m", "pip", "--version"],
                                                  capture_output=True).returncode != 0,
    "postgresql-client":  lambda: shutil.which("psql") is None,
}
# Python packages: importable name → (pip name, apt fallback)
_PY_PKGS = {
    "yaml": ("PyYAML", "python3-yaml"),
}


def _apt_install(*pkgs):
    subprocess.run(["sudo", "apt-get", "install", "-y", "-q", *pkgs],
                   capture_output=True)


def _ensure_system_deps():
    needed = [pkg for pkg, check in _APT_PKGS.items() if check()]
    if needed:
        print(f"  [..] Installing system packages: {', '.join(needed)}")
        _apt_install(*needed)
        missing = [p for p in needed
                   if p == "postgresql-client" and not shutil.which("psql")]
        if missing:
            print(f"  [WARN] Could not install: {', '.join(missing)}", file=sys.stderr)
        else:
            print(f"  [OK] System packages ready.")


def _ensure_python_deps():
    req = _os.path.join(_SCRIPT_DIR, "requirements.txt")
    if not _os.path.exists(req):
        return
    import importlib
    missing_py = []
    for mod, (pip_name, _apt) in _PY_PKGS.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_py.append((mod, pip_name, _apt))
    if not missing_py:
        return
    print(f"  [..] Installing Python requirements...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("  [OK] Python requirements installed.")
        return
    # pip failed — fall back to apt for each package
    for _mod, _pip, apt_pkg in missing_py:
        _apt_install(apt_pkg)
    try:
        for mod, (_pip, _apt) in _PY_PKGS.items():
            importlib.import_module(mod)
        print("  [OK] Python requirements installed via apt.")
    except ImportError:
        print("  [WARN] Could not install Python requirements.", file=sys.stderr)
        print("         Run: sudo apt-get install python3-pip python3-yaml", file=sys.stderr)


_ensure_system_deps()
_ensure_python_deps()

from pg_db_manager import (
    ensure_configured,
    get_config,
    preflight,
    cmd_list,
    cmd_create,
    cmd_delete,
    cmd_restore,
    cmd_users,
)


def main():
    ensure_configured()
    cfg = get_config()
    preflight(cfg)

    parser = argparse.ArgumentParser(
        prog="manager.py",
        description="Manage databases on a Crunchy PGO PostgreSQL cluster.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="If not configured, setup runs automatically on first run.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List databases and show connection info")
    p_list.add_argument("--db", help="Database to show connection info (skips prompt)")

    p_create = sub.add_parser("create", help="Create a new database and user")
    p_create.add_argument("--db", help="Database name")
    p_create.add_argument("--user", help="Username")
    p_create.add_argument("--password", help="Password (auto-generated if omitted)")

    p_delete = sub.add_parser("delete", help="Delete a database")
    p_delete.add_argument("--db", help="Database name")
    p_delete.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    p_restore = sub.add_parser("restore", help="Restore a database from pgBackRest backup")
    p_restore.add_argument("--backup", help="Backup label (default: latest)")
    p_restore.add_argument("--source-db", dest="source_db", help="Source database name in backup")
    p_restore.add_argument("--as", dest="restore_as", help="Name for the restored database")
    p_restore.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    p_users = sub.add_parser("users", help="Manage PostgreSQL users")
    usub = p_users.add_subparsers(dest="users_cmd", required=True)

    usub.add_parser("list", help="List users")

    p_ucreate = usub.add_parser("create", help="Create a user")
    p_ucreate.add_argument("--user", help="Username")
    p_ucreate.add_argument("--db", help="Database to grant access to")
    p_ucreate.add_argument("--password", help="Password (auto-generated if omitted)")

    p_udel = usub.add_parser("delete", help="Delete a user")
    p_udel.add_argument("--user", help="Username")
    p_udel.add_argument("--yes", action="store_true", help="Skip confirmation")

    p_ureset = usub.add_parser("reset-password", help="Reset a user's password")
    p_ureset.add_argument("--user", help="Username")
    p_ureset.add_argument("--password", help="New password (auto-generated if omitted)")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    dispatch = {
        "list":    cmd_list,
        "create":  cmd_create,
        "delete":  cmd_delete,
        "restore": cmd_restore,
        "users":   cmd_users,
    }
    dispatch[args.cmd](cfg, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Aborted.\n")
        sys.exit(0)
