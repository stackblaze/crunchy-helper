#!/usr/bin/env python3
"""pg-db-manager — Manage databases on a Crunchy PGO PostgreSQL cluster.

Usage:
  ./manager.py                                        # interactive Textual TUI
  ./manager.py list [--db NAME]
  ./manager.py create [--db NAME] [--user USER] [--password PASS]
  ./manager.py delete [--db NAME] [--yes]
  ./manager.py restore [--backup LABEL] [--source-db NAME] [--as NAME] [--yes]
  ./manager.py users list
  ./manager.py users create [--user USER] [--db NAME] [--password PASS]
  ./manager.py users delete [--user USER] [--yes]
  ./manager.py users reset-password [--user USER] [--password PASS]
  ./manager.py primary [--show] [--to POD_OR_NODE] [--yes]
  ./manager.py install [--user] [--name CMD] [--prefix DIR] [--force]

Running with no arguments launches the interactive TUI. Subcommands keep
their plain-stdout behaviour so they're safe in scripts and cron jobs.

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
# Python packages: importable name → (pip name, apt fallback or None).
# `apt fallback` is only useful for packages whose distro version is recent
# enough to be functional. For Textual that's not the case (Ubuntu ships
# 0.1.13 from 2022, our app needs >=0.80), so we leave the fallback empty
# and rely on pip — with --break-system-packages on PEP 668 systems.
_PY_PKGS = {
    "yaml":    ("PyYAML",  "python3-yaml"),
    "textual": ("textual", None),
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


def _pip_install(req_path: str) -> tuple[int, str]:
    """Run `pip install -r req_path`, transparently handling PEP 668.

    On Debian/Ubuntu 23.04+ pip refuses to touch the system site-packages by
    default (PEP 668). Detect the marker file and add --break-system-packages
    so the bootstrap still works on a fresh box without forcing the user to
    juggle a venv. The flag is silently ignored by pre-PEP-668 pips that
    don't know about it -- but to keep that contract we only pass it when we
    can actually see the marker, so older pips never see an unknown flag.
    """
    cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", req_path]
    pep668_marker = _os.path.join(
        _os.path.dirname(_os.__file__), "EXTERNALLY-MANAGED")
    if _os.path.exists(pep668_marker):
        cmd.append("--break-system-packages")
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stderr or r.stdout or "").strip()


def _ensure_python_deps():
    req = _os.path.join(_SCRIPT_DIR, "requirements.txt")
    if not _os.path.exists(req):
        return
    import importlib
    missing_py = []
    for mod, (pip_name, apt_pkg) in _PY_PKGS.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_py.append((mod, pip_name, apt_pkg))
    if not missing_py:
        return
    print(f"  [..] Installing Python requirements: "
          f"{', '.join(p for _m, p, _a in missing_py)}")
    rc, err = _pip_install(req)
    if rc == 0:
        print("  [OK] Python requirements installed.")
        return
    # pip failed -- fall back to apt for any package that has a usable apt
    # fallback declared. Anything without one (e.g. textual) just stays
    # missing and we surface a clear hint.
    apt_targets = [a for _m, _p, a in missing_py if a]
    if apt_targets:
        for pkg in apt_targets:
            _apt_install(pkg)
    still_missing = []
    for mod, pip_name, apt_pkg in missing_py:
        try:
            importlib.import_module(mod)
        except ImportError:
            still_missing.append((mod, pip_name, apt_pkg))
    if not still_missing:
        print("  [OK] Python requirements installed via apt.")
        return
    print("  [WARN] Could not install Python requirements:", file=sys.stderr)
    for mod, pip_name, _apt in still_missing:
        print(f"         - {pip_name} (import {mod})", file=sys.stderr)
    if err:
        print(f"         pip said: {err.splitlines()[-1]}", file=sys.stderr)
    print("         Try:  pip install --break-system-packages -r "
          f"{req}", file=sys.stderr)


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
    cmd_primary,
)


def _launch_tui(cfg) -> int:
    """Start the Textual app. Returns the app's exit code (0 = clean exit)."""
    from pg_db_manager.textual_app import run_app
    return run_app(cfg)


def _install_symlink(target_dir: str, link_name: str,
                     force: bool = False) -> int:
    """Symlink this script into a PATH directory so it's callable by name.

    The link points back at the absolute path of the running script, so
    `git pull` upgrades the installed command in place — there's nothing
    to re-install. Existing symlinks are replaced when ``force=True``.
    Returns a process exit code so the CLI can use it directly.
    """
    src = _os.path.abspath(__file__)
    target_dir = _os.path.expanduser(target_dir)
    link = _os.path.join(target_dir, link_name)

    if not _os.path.isdir(target_dir):
        try:
            _os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            print(f"  [ERR] Cannot create {target_dir}: {e}", file=sys.stderr)
            return 1

    if _os.path.lexists(link):
        # Already pointing at us? Nothing to do.
        try:
            existing = _os.readlink(link)
        except OSError:
            existing = None
        if existing == src and not force:
            print(f"  [OK] Already installed: {link} -> {src}")
            _path_hint(target_dir)
            return 0
        if not force:
            print(f"  [ERR] {link} already exists "
                  f"(use --force to overwrite).", file=sys.stderr)
            return 1
        try:
            _os.remove(link)
        except OSError as e:
            print(f"  [ERR] Cannot remove existing {link}: {e}",
                  file=sys.stderr)
            return 1

    try:
        _os.symlink(src, link)
    except PermissionError:
        print(f"  [ERR] Permission denied writing {link}.\n"
              f"        Re-run with sudo, or use:  "
              f"./manager.py install --user", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"  [ERR] Cannot create symlink: {e}", file=sys.stderr)
        return 1

    print(f"  [OK] Installed: {link} -> {src}")
    _path_hint(target_dir)
    return 0


def _path_hint(target_dir: str) -> None:
    """Warn if the install dir isn't on PATH, with a copy-pasteable fix."""
    path_dirs = _os.environ.get("PATH", "").split(_os.pathsep)
    if _os.path.realpath(target_dir) in (_os.path.realpath(p)
                                         for p in path_dirs if p):
        return
    shell_rc = "~/.bashrc"
    if _os.environ.get("ZSH_VERSION") or _os.environ.get("SHELL", "").endswith("zsh"):
        shell_rc = "~/.zshrc"
    print(f"  [..] {target_dir} is not on $PATH yet. To add it:\n"
          f"       echo 'export PATH=\"{target_dir}:$PATH\"' >> {shell_rc}\n"
          f"       source {shell_rc}")


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse tree.

    Pulled out of main() so we can render --help without first running
    the config / preflight chain (which would block on a setup prompt
    on a fresh box)."""
    parser = argparse.ArgumentParser(
        prog="pg-db-manager",
        description="Manage databases on a Crunchy PGO PostgreSQL cluster.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with no arguments to launch the interactive TUI.\n"
               "If not configured, setup runs automatically on first run.",
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
    p_restore.add_argument("--source-db", dest="source_db",
                           help="Source database name in backup")
    p_restore.add_argument("--as", dest="restore_as",
                           help="Name for the restored database")
    p_restore.add_argument("--yes", action="store_true",
                           help="Skip confirmation prompt")

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
    p_ureset.add_argument("--password",
                          help="New password (auto-generated if omitted)")

    p_primary = sub.add_parser(
        "primary", help="Show Patroni topology / switch the leader")
    p_primary.add_argument("--show", action="store_true",
                           help="Just print the topology and exit")
    p_primary.add_argument("--to",
                           help="Target pod or Kubernetes node name")
    p_primary.add_argument("--yes", action="store_true",
                           help="Skip confirmation prompt")

    # ``install`` is also exposed via argparse so ``--help`` lists it; the
    # actual handling is done up-front in main() to avoid the config /
    # preflight chain.
    p_install = sub.add_parser(
        "install",
        help="Symlink this script into a PATH dir (no more './manager.py')")
    p_install.add_argument("--name",   default="pg-db-manager",
        help="Command name to install (default: pg-db-manager)")
    p_install.add_argument("--prefix", default="/usr/local/bin",
        help="Directory to install into (default: /usr/local/bin)")
    p_install.add_argument("--user",   action="store_true",
        help="Install into ~/.local/bin instead (no sudo needed)")
    p_install.add_argument("--force",  action="store_true",
        help="Replace an existing file/symlink at the target path")

    return parser


def _print_top_level_help() -> None:
    _build_parser().print_help()


def _handle_install(argv: list) -> int:
    """Parse and run the `install` subcommand without touching cfg/preflight.

    Kept separate from the main argparse tree because `install` runs on
    fresh checkouts where no env file / kubeconfig exists yet. The flag
    set mirrors the parser registered in main() so `--help` still works.
    """
    p = argparse.ArgumentParser(
        prog="manager.py install",
        description="Symlink this script into a PATH dir.")
    p.add_argument("--name",   default="pg-db-manager")
    p.add_argument("--prefix", default="/usr/local/bin")
    p.add_argument("--user",   action="store_true")
    p.add_argument("--force",  action="store_true")
    args = p.parse_args(argv)
    target_dir = "~/.local/bin" if args.user else args.prefix
    return _install_symlink(target_dir, args.name, force=args.force)


def main():
    # `install` and bare `--help` have to work on a freshly-cloned checkout
    # that has no env file or kubeconfig yet, so we short-circuit them
    # before the config / preflight chain that every other path runs
    # through. Otherwise `pg-db-manager --help` would block on the
    # interactive setup prompt.
    if len(sys.argv) >= 2 and sys.argv[1] == "install":
        sys.exit(_handle_install(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        _print_top_level_help()
        sys.exit(0)

    ensure_configured()
    cfg = get_config()

    # No subcommand -> launch the interactive Textual app. Skip the strict
    # primary-node preflight here because the app is meant to be runnable
    # from anywhere (workstation, bastion, etc.); individual destructive
    # operations re-check primary-node access at the moment they run.
    if len(sys.argv) == 1:
        preflight(cfg, require_primary_node=False)
        sys.exit(_launch_tui(cfg))

    preflight(cfg)

    # Note: ``len(sys.argv) == 1`` was handled above (it routes to the TUI),
    # so we always have a subcommand by the time we parse here.
    args = _build_parser().parse_args()

    dispatch = {
        "list":    cmd_list,
        "create":  cmd_create,
        "delete":  cmd_delete,
        "restore": cmd_restore,
        "users":   cmd_users,
        "primary": cmd_primary,
    }
    dispatch[args.cmd](cfg, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Aborted.\n")
        sys.exit(0)
