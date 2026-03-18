"""Database commands: list, create, delete."""

import secrets
import subprocess

import yaml

from .config import ask_reconfigure_then_die, die, divider, info, ok, require, warn
from .kube import get_secret_password, kube, run_sql_direct, run_sql_super
from .cluster import patch_spec_remove_db


def cmd_list(cfg: dict, args):
    print(f"\n  Databases on {cfg['pg_host']}:\n")

    out, code = run_sql_super(cfg, "postgres",
        "SELECT datname, pg_get_userbyid(datdba), pg_size_pretty(pg_database_size(datname)) "
        "FROM pg_database WHERE datistemplate = false ORDER BY datname;")
    if code != 0 or "error:" in out.lower():
        ask_reconfigure_then_die(f"Could not query databases:\n{out}")

    rows = [r for r in out.splitlines() if r.strip()]

    print(f"  {'':4}  {'DATABASE':<35} {'OWNER':<20} SIZE")
    print(f"  {'----':4}  {'-----------------------------------':<35} {'--------------------':<20} --------")
    for i, row in enumerate(rows):
        parts = [p.strip() for p in row.split("|")]
        if len(parts) >= 3:
            print(f"  {f'{i+1})':4}  {parts[0]:<35} {parts[1]:<20} {parts[2]}")
    print()

    sel = args.db or input("  Select a database for connection info (or press Enter to skip): ").strip()
    if not sel:
        return

    if sel.isdigit():
        idx = int(sel) - 1
        if 0 <= idx < len(rows):
            parts = [p.strip() for p in rows[idx].split("|")]
            db_name, db_owner = parts[0], parts[1]
        else:
            return
    else:
        db_name = sel
        db_owner = next(
            (r.split("|")[1].strip() for r in rows if r.split("|")[0].strip() == sel), "")

    pw = get_secret_password(cfg, f"{cfg['cluster']}-pguser-{db_owner}")
    if not pw:
        pw = get_secret_password(cfg, f"{cfg['cluster']}-pguser-{db_name}")
        if pw:
            db_owner = db_name
    if not pw:
        pw = "<password>"

    print()
    divider()
    print(f"  Connection Info  :  {db_name}")
    divider()
    print(f"  Host     : {cfg['pg_host']}")
    print(f"  Port     : {cfg['pg_port']}")
    print(f"  Database : {db_name}")
    print(f"  User     : {db_owner}")
    print(f"  Password : {pw}")
    print()
    print(f"  URL      : postgresql://{db_owner}:{pw}@{cfg['pg_host']}:{cfg['pg_port']}/{db_name}")
    print()
    print(f"  JDBC     : jdbc:postgresql://{cfg['pg_host']}:{cfg['pg_port']}/{db_name}"
          f"?user={db_owner}&password={pw}")
    divider()
    print()


def write_pguser_secret(cfg: dict, db_name: str, db_user: str, db_pass: str):
    """Write a pguser-compatible credential secret to Kubernetes."""
    host = cfg["pg_host"]
    port = str(cfg["pg_port"])
    pgbouncer_host = f"{cfg['cluster']}-pgbouncer.{cfg['namespace']}.svc"
    secret_name = f"{cfg['cluster']}-pguser-{db_user}"
    string_data = {
        "host":              host,
        "port":              port,
        "dbname":            db_name,
        "user":              db_user,
        "password":          db_pass,
        "uri":               f"postgresql://{db_user}:{db_pass}@{host}:{port}/{db_name}",
        "jdbc-uri":          f"jdbc:postgresql://{host}:{port}/{db_name}?password={db_pass}&user={db_user}",
        "pgpass":            f"{host}:{port}:{db_name}:{db_user}:{db_pass}",
        "pgbouncer-host":    pgbouncer_host,
        "pgbouncer-port":    port,
        "pgbouncer-uri":     f"postgresql://{db_user}:{db_pass}@{pgbouncer_host}:{port}/{db_name}",
        "pgbouncer-jdbc-uri": f"jdbc:postgresql://{pgbouncer_host}:{port}/{db_name}"
                              f"?password={db_pass}&prepareThreshold=0&user={db_user}",
    }
    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": cfg["namespace"],
            "labels": {
                "postgres-operator.crunchydata.com/cluster": cfg["cluster"],
                "postgres-operator.crunchydata.com/pguser": db_user,
            },
        },
        "stringData": string_data,
    }
    payload = yaml.dump(secret_manifest).encode()
    r = subprocess.run(
        ["kubectl", "-n", cfg["namespace"], "apply", "-f", "-"],
        input=payload, capture_output=True,
    )
    if r.returncode != 0:
        warn(f"Could not write credential secret: {r.stderr.decode().strip()}")


def cmd_create(cfg: dict, args):
    db_name = require(args.db, "Database name")
    db_user = require(args.user, "Username")
    db_pass = args.password or input("  Password (blank = auto-generate): ").strip()
    if not db_pass:
        db_pass = secrets.token_urlsafe(15)
    print()

    out, _ = run_sql_super(cfg, "postgres",
        f"SELECT 1 FROM pg_database WHERE datname = '{db_name}';")
    if out.strip() == "1":
        die(f"Database '{db_name}' already exists.")

    out, _ = run_sql_super(cfg, "postgres",
        f"SELECT 1 FROM pg_roles WHERE rolname = '{db_user}';")
    if out.strip() == "1":
        die(f"User '{db_user}' already exists.")

    divider()
    print(f"  Creating  : {db_name}")
    print(f"  User      : {db_user}")
    print(f"  Password  : {db_pass}")
    divider()
    print()

    info("Creating user...")
    out, code = run_sql_super(cfg, "postgres",
        f"CREATE USER \"{db_user}\" WITH PASSWORD '{db_pass}';")
    if code != 0:
        die(f"Failed to create user: {out}")
    ok("User created")

    info("Creating database...")
    out, code = run_sql_super(cfg, "postgres",
        f'CREATE DATABASE "{db_name}" OWNER "{db_user}";')
    if code != 0:
        die(f"Failed to create database: {out}")
    ok("Database created")

    info("Setting permissions...")
    out, code = run_sql_super(cfg, db_name,
        f'GRANT ALL ON SCHEMA public TO "{db_user}"; '
        f'ALTER SCHEMA public OWNER TO "{db_user}"; '
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{db_user}"; '
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{db_user}";')
    if code != 0:
        die(f"Failed to set permissions: {out}")
    ok("Permissions set")

    info("Writing credential secret...")
    write_pguser_secret(cfg, db_name, db_user, db_pass)
    ok("Secret written")

    info("Verifying connection...")
    out, code = run_sql_direct(cfg, db_user, db_pass, db_name,
                               "SELECT current_database() || ' | ' || current_user;")
    if db_name in out:
        ok("Connection verified")
    else:
        warn(f"Connection test inconclusive. "
             f"Try: psql postgresql://{db_user}:<password>@{cfg['pg_host']}:{cfg['pg_port']}/{db_name}")

    print()
    divider()
    print("  DATABASE READY")
    divider()
    print(f"  Host      : {cfg['pg_host']}")
    print(f"  Port      : {cfg['pg_port']}")
    print(f"  Database  : {db_name}")
    print(f"  Username  : {db_user}")
    print(f"  Password  : {db_pass}")
    print()
    print(f"  postgresql://{db_user}:{db_pass}@{cfg['pg_host']}:{cfg['pg_port']}/{db_name}")
    divider()
    print()


def cmd_delete(cfg: dict, args):
    out, code = run_sql_super(cfg, "postgres",
        "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname;")
    if code != 0:
        ask_reconfigure_then_die(f"Could not list databases:\n{out}")
    rows = [r.strip() for r in out.splitlines() if r.strip()]

    if args.db:
        db_name = args.db
    else:
        print("\n  Current databases:\n")
        for i, db in enumerate(rows):
            print(f"  {i + 1})  {db}")
        print()
        sel = input("  Enter number or database name to delete: ").strip()
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(rows):
                db_name = rows[idx]
            else:
                die("Invalid number selected.")
        else:
            db_name = sel

    print(f"\n  WARNING: This will permanently delete database '{db_name}'\n")

    if not args.yes:
        confirm = input(f"  Type  {db_name}  to confirm: ").strip()
        print()
        if confirm != db_name:
            print("  [ABORT] Names do not match — nothing deleted.\n")
            return

    owner_out, _ = run_sql_super(cfg, "postgres",
        f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{db_name}';")
    db_owner = owner_out.strip()

    info("Updating cluster spec...")
    patch_spec_remove_db(cfg, db_name)
    ok("Cluster spec updated")

    info("Terminating active connections...")
    run_sql_super(cfg, "postgres",
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();")

    info("Dropping database...")
    drop_out, drop_code = run_sql_super(cfg, "postgres", f'DROP DATABASE IF EXISTS "{db_name}";')
    if drop_code != 0 or "error" in drop_out.lower():
        die(f"DROP DATABASE failed:\n  {drop_out}")
    ok("Database dropped")

    if db_owner and db_owner != cfg["admin_user"]:
        remaining_out, _ = run_sql_super(cfg, "postgres",
            f"SELECT count(*) FROM pg_database WHERE datistemplate = false "
            f"AND pg_get_userbyid(datdba) = '{db_owner}';")
        if remaining_out.strip() == "0":
            info(f"Dropping user '{db_owner}'...")
            run_sql_super(cfg, "postgres",
                f'DROP OWNED BY "{db_owner}"; '
                f'DROP USER IF EXISTS "{db_owner}";')
            kube("-n", cfg["namespace"], "delete", "secret",
                 f"{cfg['cluster']}-pguser-{db_owner}", "--ignore-not-found")
            ok(f"User '{db_owner}' and credentials removed")
        else:
            info(f"User '{db_owner}' retained (owns other databases)")

    print()
    divider()
    print(f"  Database '{db_name}' deleted successfully")
    divider()
    print()
