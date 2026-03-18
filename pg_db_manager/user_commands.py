"""User commands: list, create, delete, reset-password."""

import base64
import getpass
import json
import secrets

from .config import die, divider, info, ok, require
from .db_commands import write_pguser_secret
from .kube import kube, run_sql_super


def cmd_users(cfg: dict, args):
    subcmd = args.users_cmd

    if subcmd == "list":
        print(f"\n  Users on {cfg['pg_host']}:\n")
        out, _ = run_sql_super(cfg, "postgres",
            "SELECT rolname, CASE WHEN rolsuper THEN 'superuser' "
            "WHEN rolcreatedb AND rolcreaterole THEN 'createdb+createrole' "
            "WHEN rolcreatedb THEN 'createdb' WHEN rolcreaterole THEN 'createrole' "
            "ELSE 'normal' END "
            "FROM pg_roles WHERE rolcanlogin = true ORDER BY rolname;")
        rows = [r for r in out.splitlines() if r.strip()]
        print(f"  {'':4}  {'USERNAME':<30} ROLE")
        print(f"  {'----':4}  {'------------------------------':<30} ----------")
        for i, row in enumerate(rows):
            parts = [p.strip() for p in row.split("|")]
            if len(parts) >= 2:
                print(f"  {f'{i+1})':4}  {parts[0]:<30} {parts[1]}")
        print()

    elif subcmd == "create":
        new_user = require(args.user, "New username")
        if not new_user:
            die("Username cannot be empty.")
        new_pass = args.password or getpass.getpass("  Password (Enter to auto-generate): ")
        if not new_pass:
            new_pass = secrets.token_urlsafe(15)
            print(f"  [INFO] Auto-generated password: {new_pass}")

        exists_out, _ = run_sql_super(cfg, "postgres",
            f"SELECT 1 FROM pg_roles WHERE rolname='{new_user}';")
        if exists_out.strip() == "1":
            die(f"User '{new_user}' already exists.")

        db_out, _ = run_sql_super(cfg, "postgres",
            "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname;")
        dbs = [r.strip() for r in db_out.splitlines() if r.strip()]

        if args.db:
            new_db = args.db
        else:
            print("\n  Grant full access on which database?")
            for i, db in enumerate(dbs):
                print(f"  {i + 1})  {db}")
            print()
            sel = input("  Database: ").strip()
            new_db = dbs[int(sel) - 1] if sel.isdigit() else sel

        if not new_db:
            die("Invalid database selection.")

        info("Creating user and granting access...")
        run_sql_super(cfg, "postgres",
            f"CREATE USER \"{new_user}\" WITH PASSWORD '{new_pass}';")
        run_sql_super(cfg, "postgres",
            f'GRANT ALL PRIVILEGES ON DATABASE "{new_db}" TO "{new_user}";')
        run_sql_super(cfg, new_db,
            f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{new_user}"; '
            f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{new_user}"; '
            f'GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO "{new_user}"; '
            f'GRANT USAGE, CREATE ON SCHEMA public TO "{new_user}"; '
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{new_user}"; '
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{new_user}";')

        info("Writing credential secret...")
        write_pguser_secret(cfg, new_db, new_user, new_pass)
        ok("Secret written")

        print()
        divider()
        print(f"  User created   : {new_user}")
        print(f"  Password       : {new_pass}")
        print(f"  Database       : {new_db}")
        print(f"  Access         : FULL")
        print()
        print(f"  URL  : postgresql://{new_user}:{new_pass}@{cfg['pg_host']}:{cfg['pg_port']}/{new_db}")
        print(f"  JDBC : jdbc:postgresql://{cfg['pg_host']}:{cfg['pg_port']}/{new_db}"
              f"?user={new_user}&password={new_pass}")
        divider()
        print()

    elif subcmd == "delete":
        del_out, _ = run_sql_super(cfg, "postgres",
            f"SELECT usename FROM pg_user "
            f"WHERE usename NOT IN ('postgres', '{cfg['admin_user']}') ORDER BY usename;")
        del_users = [r.strip() for r in del_out.splitlines() if r.strip()]

        if not del_users:
            print("  No deletable users found.\n")
            return

        if args.user:
            del_user = args.user
        else:
            for i, u in enumerate(del_users):
                print(f"  {i + 1})  {u}")
            print()
            sel = input("  Select user to delete: ").strip()
            del_user = del_users[int(sel) - 1] if sel.isdigit() else sel

        if not del_user:
            die("Invalid selection.")

        if not args.yes:
            confirm = input(f"  Confirm delete user '{del_user}'? [y/N]: ").strip()
            if confirm.lower() != "y":
                print("  Cancelled.\n")
                return

        run_sql_super(cfg, "postgres",
            f'REASSIGN OWNED BY "{del_user}" TO postgres; '
            f'DROP OWNED BY "{del_user}"; '
            f'DROP USER "{del_user}";')
        kube("-n", cfg["namespace"], "delete", "secret",
             f"{cfg['cluster']}-pguser-{del_user}", "--ignore-not-found")
        ok(f"User '{del_user}' deleted.\n")

    elif subcmd == "reset-password":
        reset_out, _ = run_sql_super(cfg, "postgres",
            "SELECT usename FROM pg_user WHERE usename NOT IN ('postgres') ORDER BY usename;")
        reset_users = [r.strip() for r in reset_out.splitlines() if r.strip()]

        if not reset_users:
            print("  No users found.\n")
            return

        if args.user:
            reset_user = args.user
        else:
            for i, u in enumerate(reset_users):
                print(f"  {i + 1})  {u}")
            print()
            sel = input("  Select user to reset password: ").strip()
            reset_user = reset_users[int(sel) - 1] if sel.isdigit() else sel

        reset_pass = args.password or getpass.getpass("  New password (Enter to auto-generate): ")
        if not reset_pass:
            reset_pass = secrets.token_urlsafe(15)
            print(f"  [INFO] Auto-generated password: {reset_pass}")

        run_sql_super(cfg, "postgres",
            f"ALTER USER \"{reset_user}\" PASSWORD '{reset_pass}';")

        secret_name = f"{cfg['cluster']}-pguser-{reset_user}"
        r = kube("-n", cfg["namespace"], "get", "secret", secret_name, "-o", "name")
        if r.returncode == 0:
            encoded = base64.b64encode(reset_pass.encode()).decode()
            patch = json.dumps([{"op": "replace", "path": "/data/password", "value": encoded}])
            kube("-n", cfg["namespace"], "patch", "secret", secret_name,
                 "--type=json", f"-p={patch}")
            ok("Credential secret updated.")

        print()
        divider()
        print(f"  User         : {reset_user}")
        print(f"  New password : {reset_pass}")
        print(f"  URL          : postgresql://{reset_user}:{reset_pass}"
              f"@{cfg['pg_host']}:{cfg['pg_port']}/<database>")
        divider()
        print()
