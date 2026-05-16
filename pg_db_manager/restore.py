"""Restore command: PVC + restore pod + extract pod + kubectl cp (same as restore-db.sh)."""

import datetime
import json
import os
import re
import subprocess
import time

import yaml

from .config import die, divider, info, ok, warn
from .kube import get_primary_container_id, get_primary_node, get_primary_pod_spec, kube, pgbackrest_exec, run_sql_super


def _s3_env_prefix(cfg: dict) -> str:
    """Return shell env-var prefix to inject S3 credentials into a pgbackrest command.

    Credentials stored in cfg by _fix_pgbackrest_s3_credentials are passed this way so
    the retry and the restore pod both work without waiting for a pod restart.
    """
    if not cfg.get("_s3_access_key"):
        return ""
    repo = cfg.get("_s3_repo", "repo1").upper()
    # Shell-quote the values in case they contain special characters
    def sq(v):
        return "'" + v.replace("'", "'\\''") + "'"
    return (
        f"PGBACKREST_{repo}_S3_KEY={sq(cfg['_s3_access_key'])} "
        f"PGBACKREST_{repo}_S3_KEY_SECRET={sq(cfg['_s3_secret_key'])} "
    )


def _fix_pgbackrest_s3_credentials(cfg: dict):
    """
    Detect missing S3 credentials, prompt the user, create the Kubernetes secret,
    patch the PostgresCluster spec, and store credentials in cfg so they can be
    injected immediately as env vars — no pod restart required for this session.
    On future runs PGO will have mounted the secret, so no prompt is needed.
    """
    ns = cfg["namespace"]
    cluster = cfg["cluster"]

    warn("pgbackrest is missing S3 credentials (repo1-s3-key).")
    print()
    print("  The cluster is configured to use S3 for backups but the S3 access")
    print("  credentials are not available inside the database pod.")
    print()

    # Read the cluster spec to find the repo S3 config (bucket, endpoint, region)
    r = kube("-n", ns, "get", "postgrescluster", cluster, "-o", "json")
    if r.returncode != 0:
        die("Could not read PostgresCluster spec.")
    spec = json.loads(r.stdout.decode())
    pgbackrest_spec = spec.get("spec", {}).get("backups", {}).get("pgbackrest", {})
    repos = pgbackrest_spec.get("repos", [])

    repo_name = "repo1"
    s3_cfg = None
    for repo in repos:
        if "s3" in repo:
            s3_cfg = repo["s3"]
            repo_name = repo.get("name", "repo1")
            break

    if s3_cfg:
        print(f"  S3 bucket   : {s3_cfg.get('bucket', '(unknown)')}")
        print(f"  S3 endpoint : {s3_cfg.get('endpoint', '(unknown)')}")
        print(f"  S3 region   : {s3_cfg.get('region', '(unknown)')}")
        print()
    else:
        print("  (Could not find S3 repo config in the PostgresCluster spec.)")
        print()

    # Prompt for credentials
    print("  Enter your S3 credentials to configure pgbackrest:")
    print()
    access_key = input("  AWS Access Key ID     : ").strip()
    secret_key = input("  AWS Secret Access Key : ").strip()
    if not access_key or not secret_key:
        die("S3 credentials cannot be empty.")

    # Store in cfg so _s3_env_prefix() can inject them into every pgbackrest call
    # this session — no pod restart needed.
    cfg["_s3_access_key"] = access_key
    cfg["_s3_secret_key"] = secret_key
    cfg["_s3_repo"] = repo_name

    # Build and apply the secret.
    # Individual keys (s3-access-key / s3-secret-key) let the restore pod pull them
    # via secretKeyRef env vars without exposing them in the pod spec args.
    secret_name = f"{cluster}-pgbackrest-s3-creds"
    cfg["_s3_secret_name"] = secret_name
    s3_conf = f"[global]\n{repo_name}-s3-key={access_key}\n{repo_name}-s3-key-secret={secret_key}\n"
    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": ns},
        "stringData": {
            "s3.conf": s3_conf,
            "s3-access-key": access_key,
            "s3-secret-key": secret_key,
        },
    }
    r = kube("-n", ns, "apply", "-f", "-", input_data=yaml.dump(secret_manifest).encode())
    if r.returncode != 0:
        die(f"Failed to create S3 credentials secret: {r.stderr.decode().strip()}")
    ok(f"Secret '{secret_name}' created/updated")

    # Patch the cluster spec to reference the secret (if not already listed).
    # PGO will mount it into the pgbackrest-config projected volume on next reconcile;
    # for this session credentials are injected via env vars instead.
    existing_configs = pgbackrest_spec.get("configuration", [])
    existing_secret_names = [c["secret"]["name"] for c in existing_configs if "secret" in c]
    if secret_name not in existing_secret_names:
        existing_configs.append({"secret": {"name": secret_name}})
        patch = {"spec": {"backups": {"pgbackrest": {"configuration": existing_configs}}}}
        r = kube("-n", ns, "patch", "postgrescluster", cluster,
                 "--type=merge", "-p", json.dumps(patch))
        if r.returncode != 0:
            die(f"Failed to patch PostgresCluster spec: {r.stderr.decode().strip()}")
        ok(f"PostgresCluster spec patched — referencing '{secret_name}'")
        ok("Credentials will be mounted automatically on the next pod restart")
    else:
        ok("Cluster spec already references the credentials secret — contents updated")
    print()


def cmd_restore(cfg: dict, args):
    container_id = get_primary_container_id(cfg)
    restore_date = time.strftime("%Y%m%d%H%M%S")

    def _pgbackrest_info_cmd():
        return (
            f"{_s3_env_prefix(cfg)}"
            "pgbackrest --config=/etc/pgbackrest.conf "
            "--config-include-path=/etc/pgbackrest/conf.d "
            "--stanza=db info --output=json 2>&1"
        )

    r = pgbackrest_exec(cfg, container_id, _pgbackrest_info_cmd())

    if r.returncode != 0:
        err_text = r.stdout.strip() or r.stderr.strip()
        if "s3-key" in err_text.lower() or "repo1-s3-key" in err_text:
            print()
            _fix_pgbackrest_s3_credentials(cfg)
            # Retry with credentials now in cfg — same container, no restart needed
            r = pgbackrest_exec(cfg, container_id, _pgbackrest_info_cmd())
            if r.returncode != 0:
                die(f"pgbackrest still unreachable after credential fix.\n  Details: {r.stdout.strip() or r.stderr.strip()}")
        else:
            die(f"pgbackrest unreachable.\n  Details: {err_text}")

    raw = r.stdout.strip()
    if not raw:
        die("pgbackrest returned no output. Check that the primary pod is healthy.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        die(f"pgbackrest returned unexpected output:\n  {raw[:300]}")

    backups = []
    for stanza in data:
        for b in stanza.get("backup", []):
            ts = datetime.datetime.fromtimestamp(
                b["timestamp"]["stop"], datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            backup_date = datetime.datetime.fromtimestamp(
                b["timestamp"]["stop"], datetime.timezone.utc
            ).strftime("%Y%m%d")
            size_mb = round(b["info"]["size"] / 1024 / 1024, 1)
            backups.append({
                "label": b["label"],
                "date": backup_date,
                "display": f"{b['label']}  |  {b['type'].upper():<4}  |  {ts}  |  {size_mb} MB",
            })

    if not backups:
        status_msgs = [s.get("status", {}).get("message", "") for s in data]
        hint = ""
        if any("missing stanza path" in m for m in status_msgs):
            hint = ("\n  No backups exist yet — the stanza is initialised but the repo is empty.\n"
                    "  A full backup is scheduled based on the cluster's backup schedule.\n"
                    "  To trigger a manual backup now:\n\n"
                    f"    kubectl annotate postgrescluster -n {cfg['namespace']} {cfg['cluster']} \\\n"
                    "      postgres-operator.crunchydata.com/pgbackrest-backup=\"$(date '+%F_%H:%M:%S')\" \\\n"
                    "      --overwrite\n")
        die(f"No backups found.{hint}")

    # Phase 1: questions upfront
    print("\n  Available backups:\n")
    for i, b in enumerate(backups):
        print(f"  {i + 1})  {b['display']}")
    print()

    if args.backup:
        backup_label = args.backup
    else:
        sel = input("  Select backup number (or press Enter for latest): ").strip()
        if not sel:
            backup_label = backups[-1]["label"]
        elif sel.isdigit():
            backup_label = backups[int(sel) - 1]["label"]
        else:
            backup_label = sel

    backup_date = next((b["date"] for b in backups if b["label"] == backup_label), backup_label[:8])

    live_out, _ = run_sql_super(cfg, "postgres",
        "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname;")
    live_dbs = [l.strip() for l in live_out.splitlines() if l.strip()]

    print()
    print("  Which database do you want to restore from this backup?")
    print("  (Current databases shown as reference — backup likely contains the same)\n")
    for i, db in enumerate(live_dbs):
        print(f"  {i + 1})  {db}")
    print()

    if args.source_db:
        src_db = args.source_db
    else:
        sel = input("  Source database name: ").strip()
        src_db = live_dbs[int(sel) - 1] if sel.isdigit() and int(sel) <= len(live_dbs) else sel

    if not src_db:
        die("Source database name cannot be empty.")

    default_name = f"{src_db}-restored-{time.strftime('%Y-%m-%d')}"
    if args.restore_as:
        restored_db = args.restore_as
    else:
        custom = input(f"\n  Restore as (new database name) [{default_name}]: ").strip()
        restored_db = custom if custom else default_name

    # Restore plan
    print()
    divider()
    print("  RESTORE PLAN")
    pvc_name_plan = f"restore-{src_db.replace('_', '-').lower()}-{backup_date}"
    extract_pod_plan = f"extract-{src_db.replace('_', '-').lower()}-{backup_date}"
    divider()
    print(f"  Backup      : {backup_label}")
    print(f"  Backup date : {backup_date}")
    print(f"  Source DB   : {src_db}  (from backup)")
    print(f"  Restore as  : {restored_db}  (new database on live cluster)")
    print(f"  Cluster     : {cfg['cluster']}  /  {cfg['pg_host']}:{cfg['pg_port']}")
    print(f"  PVC         : {pvc_name_plan}")
    print(f"  Extract pod : {extract_pod_plan}")
    divider()
    print()
    print("  How it works (same as restore-db.sh):")
    print("    1. PVC holds restore data; reuse if same backup date already restored")
    print("    2. Restore pod: pgbackrest restore from S3 to PVC (if needed)")
    print("    3. Extract pod: temp Postgres on PVC → pg_dump → dump file on PVC")
    print("    4. Copy dump to primary; CREATE DATABASE + pg_restore on primary")
    print()
    print("  The live cluster is NOT interrupted. PVC/pod kept for reuse until you delete them.")
    print()

    if not args.yes:
        confirm = input("  Proceed? This will take a few minutes. [y/N]: ").strip()
        print()
        if confirm.lower() != "y":
            print("  Cancelled.\n")
            return

    # Phase 2: PVC + restore pod + extract pod + kubectl cp
    def k8s_name(s: str) -> str:
        return s.replace("_", "-").lower()

    primary_node = get_primary_node(cfg)
    pvc_name = f"restore-{k8s_name(src_db)}-{backup_date}"
    restore_pod_name = f"extract-{k8s_name(src_db)}-{backup_date}-restore"
    extract_pod_name = f"extract-{k8s_name(src_db)}-{backup_date}"
    friendly_date = datetime.datetime.strptime(backup_date, "%Y%m%d").strftime("%b%d").lower()
    pod_spec = get_primary_pod_spec(cfg)
    image = pod_spec["image"]
    pg_major = "16"
    m = re.search(r"[-:](\d+)\.\d+", image)
    if m:
        pg_major = m.group(1)
    data_dir_name = f"pg{pg_major}-{friendly_date}"
    data_dir = f"/restore-data/{data_dir_name}"
    dump_file_remote = f"/restore-data/{src_db}-{friendly_date}.dump"
    local_dump = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"pgrestore-{cfg['cluster']}-{restore_date}.dump")
    ns = cfg["namespace"]

    info("[1/11] Checking for existing restore data...")
    r = kube("-n", ns, "get", "pvc", pvc_name, "-o", "name")
    pvc_exists = r.returncode == 0 and pvc_name in (r.stdout or b"").decode()
    if pvc_exists:
        ok(f"PVC {pvc_name} exists — will reuse if data already restored")
    else:
        info("PVC does not exist — will create and download from S3")

    info("[2/11] Primary container: " + container_id[:12])
    info("[3/11] Backup set: " + backup_label)

    info("[4/11] Ensuring PVC...")
    if not pvc_exists:
        pvc_manifest = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "20Gi"}},
            },
        }
        r = kube("-n", ns, "apply", "-f", "-", input_data=yaml.dump(pvc_manifest).encode())
        if r.returncode != 0:
            die(f"Failed to create PVC: {r.stderr.decode().strip()}")
        ok("PVC created")
    else:
        ok("PVC already exists")

    info("[5/11] Checking if PostgreSQL data already restored on PVC...")
    check_pod_name = f"check-data-{backup_date}-{restore_date}"
    check_overrides = {
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": primary_node},
            "containers": [{
                "name": "check",
                "image": "busybox",
                "command": ["sh", "-c", f"test -f {data_dir}/global/pg_control && echo exists || echo missing"],
                "volumeMounts": [{"name": "data", "mountPath": "/restore-data"}],
            }],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}}],
        }
    }
    kube("run", check_pod_name, "--restart=Never", "--image=busybox", "-n", ns, "--overrides=" + json.dumps(check_overrides))
    deadline_check = time.time() + 60
    while time.time() < deadline_check:
        time.sleep(2)
        r = kube("-n", ns, "get", "pod", check_pod_name, "-o", "jsonpath={.status.phase}")
        phase = (r.stdout or b"").decode().strip()
        if phase in ("Succeeded", "Failed"):
            break
    r = kube("-n", ns, "logs", check_pod_name)
    data_exists_out = (r.stdout or b"").decode()
    kube("-n", ns, "delete", "pod", check_pod_name, "--wait=false")
    need_restore = "exists" not in data_exists_out

    if need_restore:
        ok("Data not found — will restore from S3")
    else:
        ok("Data already on PVC — skipping S3 restore")

    if need_restore:
        info("[6/11] Restoring from S3 (this may take 10–15 minutes)...")
        pgbackrest_volume = pod_spec["pgbackrest_volume"]
        restore_cmd = (
            f"pgbackrest --config=/etc/pgbackrest.conf --config-include-path=/etc/pgbackrest/conf.d "
            f"--stanza=db --set={backup_label} restore "
            f"--pg1-path={data_dir} --type=immediate --delta --process-max=4"
        )
        restore_container: dict = {
            "name": "restore",
            "image": image,
            "command": ["/bin/bash", "-c", f"set -e; echo 'Starting restore from S3...'; {restore_cmd}; echo 'Restore complete'"],
            "volumeMounts": [
                {"name": "pgbackrest-config", "mountPath": "/etc/pgbackrest/conf.d"},
                {"name": "restore-data", "mountPath": "/restore-data"},
            ],
        }
        # If credentials were fixed this session, inject them via secretKeyRef so
        # the restore pod has them even before PGO remounts the projected volume.
        if cfg.get("_s3_secret_name"):
            repo_upper = cfg.get("_s3_repo", "repo1").upper()
            restore_container["env"] = [
                {"name": f"PGBACKREST_{repo_upper}_S3_KEY",
                 "valueFrom": {"secretKeyRef": {"name": cfg["_s3_secret_name"], "key": "s3-access-key"}}},
                {"name": f"PGBACKREST_{repo_upper}_S3_KEY_SECRET",
                 "valueFrom": {"secretKeyRef": {"name": cfg["_s3_secret_name"], "key": "s3-secret-key"}}},
            ]
        restore_pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": restore_pod_name, "namespace": ns},
            "spec": {
                "restartPolicy": "Never",
                "nodeSelector": {"kubernetes.io/hostname": primary_node},
                "containers": [restore_container],
                "volumes": [pgbackrest_volume, {"name": "restore-data", "persistentVolumeClaim": {"claimName": pvc_name}}],
            },
        }
        r = kube("-n", ns, "apply", "-f", "-", input_data=yaml.dump(restore_pod).encode())
        if r.returncode != 0:
            die(f"Failed to create restore pod: {r.stderr.decode().strip()}")

        deadline = time.time() + 900
        last_shown_lines: list = []
        while time.time() < deadline:
            time.sleep(10)
            r = kube("-n", ns, "get", "pod", restore_pod_name, "-o", "jsonpath={.status.phase}")
            phase = (r.stdout or b"").decode().strip()

            r_logs = kube("-n", ns, "logs", restore_pod_name, "--tail=5")
            log_text = (r_logs.stdout or b"").decode().strip()
            if log_text:
                new_lines = [l for l in log_text.splitlines() if l not in last_shown_lines]
                for line in new_lines:
                    print(f"    {line}", flush=True)
                if new_lines:
                    last_shown_lines = log_text.splitlines()

            if phase == "Succeeded":
                ok("Restore from S3 completed")
                break
            if phase == "Failed":
                r2 = kube("-n", ns, "logs", restore_pod_name, "--tail=50")
                kube("-n", ns, "delete", "pod", restore_pod_name, "--wait=false")
                die(f"Restore pod failed.\n\n{(r2.stdout or b'').decode()}")
        else:
            kube("-n", ns, "delete", "pod", restore_pod_name, "--wait=false")
            die("Restore pod did not complete within 15 minutes.")

        kube("-n", ns, "delete", "pod", restore_pod_name, "--wait=false")
    else:
        ok("[6/11] Skipping S3 restore (data already exists)")

    info("[7/11] Extracting database '" + src_db + "'...")
    extract_script = f"""\
set -e
echo "Checking restore data..."
test -f {data_dir}/global/pg_control || {{ echo "ERROR: Restore data not found"; exit 1; }}
echo "Preparing data..."
rm -f {data_dir}/recovery.signal {data_dir}/standby.signal {data_dir}/backup_label {data_dir}/backup_label.old 2>/dev/null || true
pg_resetwal -f {data_dir}
echo "Starting temp PostgreSQL on 5433..."
postgres -D {data_dir} -p 5433 \\
  -c config_file={data_dir}/postgresql.conf \\
  -c hba_file={data_dir}/pg_hba.conf \\
  -c ident_file={data_dir}/pg_ident.conf \\
  -c listen_addresses=localhost -c unix_socket_directories=/tmp -c ssl=off -c logging_collector=off \\
  -c log_destination=stderr > /restore-data/postgres.log 2>&1 &
PG_PID=$!
for i in $(seq 30); do
  pg_isready -h /tmp -p 5433 -U postgres >/dev/null 2>&1 && break
  sleep 2
done
pg_isready -h /tmp -p 5433 -U postgres >/dev/null 2>&1 || {{ kill $PG_PID 2>/dev/null; cat /restore-data/postgres.log; exit 1; }}
echo "Extracting with pg_dump..."
# PG 16+ supports --exclude-schema in pg_dump; the pgbouncer schema is owned
# by PGO and conflicts with the schema created on a fresh CREATE DATABASE
# (template1 inherits it), so we strip it from the dump up front.
pg_dump -h /tmp -p 5433 -U postgres -Fc --exclude-schema=pgbouncer \\
  -d "{src_db}" -f {dump_file_remote}
kill $PG_PID 2>/dev/null || true
echo "Extraction complete!"
tail -f /dev/null
"""
    extract_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": extract_pod_name, "namespace": ns},
            "spec": {
                "restartPolicy": "Never",
                "nodeSelector": {"kubernetes.io/hostname": primary_node},
                "containers": [{
                    "name": "postgres",
                "image": image,
                "command": ["/bin/bash", "-c", extract_script],
                "volumeMounts": [{"name": "restore-data", "mountPath": "/restore-data"}],
                "resources": {"requests": {"memory": "2Gi", "cpu": "1000m"}, "limits": {"memory": "4Gi", "cpu": "2000m"}},
            }],
            "volumes": [{"name": "restore-data", "persistentVolumeClaim": {"claimName": pvc_name}}],
        },
    }
    # Idempotent extract-pod creation:
    #   - If a previous run left the pod Running and "Extraction complete!" is
    #     in the logs, reuse it (the dump file is on the PVC, ready for cp).
    #   - Otherwise (Pending/Failed/Succeeded/missing required field), delete
    #     and re-apply so we always exec against a clean pod.
    r = kube("-n", ns, "get", "pod", extract_pod_name,
             "-o", "jsonpath={.status.phase}")
    pod_phase = (r.stdout or b"").decode().strip() if r.returncode == 0 else ""
    reuse_pod = False
    if pod_phase == "Running":
        r_logs = kube("-n", ns, "logs", extract_pod_name, "--tail=20")
        if "Extraction complete!" in (r_logs.stdout or b"").decode():
            reuse_pod = True
    if not reuse_pod and pod_phase:
        info(f"Removing previous extract pod (phase={pod_phase})...")
        kube("-n", ns, "delete", "pod", extract_pod_name,
             "--wait=true", "--timeout=60s")

    if reuse_pod:
        ok("Reusing existing extract pod (dump already produced)")
    else:
        r = kube("-n", ns, "apply", "-f", "-",
                 input_data=yaml.dump(extract_pod).encode())
        if r.returncode != 0:
            die(f"Failed to create extract pod: {r.stderr.decode().strip()}")

        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(5)
            r = kube("-n", ns, "logs", extract_pod_name, "--tail=5")
            logs = (r.stdout or b"").decode()
            if "Extraction complete!" in logs:
                ok("Database extraction completed")
                break
        else:
            r2 = kube("-n", ns, "logs", extract_pod_name, "--tail=30")
            die(f"Extract pod did not finish in time.\n\n"
                f"{(r2.stdout or b'').decode()}")

    info("[8/11] Copying dump to local...")
    r = subprocess.run(
        ["kubectl", "cp", f"{ns}/{extract_pod_name}:{dump_file_remote}", local_dump],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        die(f"kubectl cp from extract pod failed: {r.stderr.strip()}")
    ok(f"Dump copied to {local_dump}")

    info("[9/11] Checking target database...")
    safe_db = restored_db.replace("'", "''")
    out, _ = run_sql_super(cfg, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{safe_db}';")
    if out.strip() == "1":
        if os.path.exists(local_dump):
            os.remove(local_dump)
        die(f"Database '{restored_db}' already exists. Choose a different name or drop it first.")
    ok("Database name available")

    info("[10/11] Creating database and restoring data on primary...")
    safe_restored = restored_db.replace('"', '""')
    create_out, create_code = run_sql_super(cfg, "postgres", f'CREATE DATABASE "{safe_restored}";')
    if create_code != 0 or "error" in create_out.lower():
        if os.path.exists(local_dump):
            os.remove(local_dump)
        die(f"CREATE DATABASE failed: {create_out.strip()}")
    run_sql_super(cfg, "postgres", f'ALTER DATABASE "{safe_restored}" OWNER TO "{cfg["admin_user"]}";')

    # The pgbouncer schema is created in template1 by PGO, so every fresh
    # CREATE DATABASE inherits it. Without this, pg_restore aborts on
    # "schema pgbouncer already exists" when restoring a pre-PGO-pgbouncer-
    # removal dump. Drop it pre-emptively; harmless if absent.
    drop_out, _ = run_sql_super(cfg, restored_db,
        "DROP SCHEMA IF EXISTS pgbouncer CASCADE;")
    if "error" in drop_out.lower() and "does not exist" not in drop_out.lower():
        warn(f"Could not drop pgbouncer schema: {drop_out.strip()}")

    info("Copying dump to primary container...")
    with open(local_dump, "rb") as f:
        r = subprocess.run(
            ["sudo", "crictl", "exec", "-i", container_id,
             "sh", "-c", "cat > /tmp/restore.dump"],
            input=f.read(), capture_output=True,
        )
    if r.returncode != 0:
        die(f"Failed to copy dump to primary container: {r.stderr.decode().strip()}")

    # pg_restore in PG 16 doesn't support --exclude-schema (that's PG 17+);
    # we handle the pgbouncer collision by stripping it from the dump
    # (above, in pg_dump --exclude-schema) and dropping the schema on the
    # target database before this call (above).
    r = subprocess.run(
        ["sudo", "crictl", "exec", "-i", container_id,
         "pg_restore", "-U", "postgres", "-d", restored_db, "-v",
         "/tmp/restore.dump"],
        capture_output=True, text=True,
    )
    subprocess.run(["sudo", "crictl", "exec", "-i", container_id,
                    "rm", "-f", "/tmp/restore.dump"], capture_output=True)
    if r.returncode != 0:
        if os.path.exists(local_dump):
            os.remove(local_dump)
        err = (r.stderr or r.stdout or "").strip()
        die(f"pg_restore failed (exit {r.returncode}):\n{err}")
    if "ERROR" in (r.stdout + r.stderr):
        warn("pg_restore reported some errors (may be benign): " + (r.stderr or r.stdout)[:200])
    ok("Data restored")

    if os.path.exists(local_dump):
        os.remove(local_dump)

    count_out, count_code = run_sql_super(cfg, restored_db,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")
    if count_code != 0 or "error" in count_out.lower():
        die(f"Restore verification failed: database '{restored_db}' not found or not accessible.\n{count_out.strip()}")

    print()
    divider()
    print("  RESTORE COMPLETE")
    divider()
    print(f"  Backup used   : {backup_label}")
    print(f"  Source DB     : {src_db}")
    print(f"  Restored as   : {restored_db}")
    print(f"  Tables found  : {count_out.strip()}")
    print(f"  Host          : {cfg['pg_host']}")
    print(f"  Port          : {cfg['pg_port']}")
    print()
    print(f"  postgresql://<user>:<password>@{cfg['pg_host']}:{cfg['pg_port']}/{restored_db}")
    divider()
    print()
    print("  Preserved (reuse for same backup date):")
    print(f"    PVC: {pvc_name}")
    print(f"    Pod: {extract_pod_name}")
    print()
    print("  To cleanup when done:")
    print(f"    kubectl delete pod -n {ns} {extract_pod_name}")
    print(f"    kubectl delete pvc -n {ns} {pvc_name}  # only if freeing space")
    divider()
    print()
