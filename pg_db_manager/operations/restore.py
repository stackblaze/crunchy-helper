"""Restore operations for the Textual UI (two-phase, kubectl-only).

Phases:

  Phase 1 - prepare a *backup session*:
      - Pick a backup label (the screen lists what pgbackrest reports).
      - Materialise a PVC and (if needed) restore the cluster files into it
        from S3 via a one-shot restore pod.
      - Start a long-lived "extract pod" that runs a temp postgres on the
        PVC and idles. The user can now query that postgres for what
        databases the *backup* actually contains.

  Phase 2 - restore one database:
      - Run pg_dump inside the extract pod for the chosen source DB
        (excluding the pgbouncer schema; PG 16+).
      - kubectl cp the dump file to a local path.
      - kubectl cp it into the primary pod.
      - CREATE DATABASE on the primary, DROP the inherited pgbouncer
        schema, run pg_restore.
      - Verify by counting public tables.

The TUI keeps the BackupSession alive for the duration of the screen, so
restoring multiple databases from the same backup doesn't redownload
from S3 or restart the temp postgres -- a frequent pain point with the
old single-shot CLI restore.

Notes vs the CLI ``cmd_restore``:
  - We use ``kubectl exec`` everywhere (no crictl), so the TUI works
    from any workstation with KUBECONFIG.
  - We do NOT prompt for missing S3 credentials here; if the cluster
    doesn't have them, ``list_backups`` raises and the screen tells
    the user to run the CLI restore once to set them up.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import yaml

from ..kube import kube
from .progress import OperationResult, ProgressReporter


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class Backup:
    label:     str
    type:      str            # FULL / DIFF / INCR
    timestamp: str            # human-readable UTC string
    date:      str            # YYYYMMDD (used in PVC/pod naming)
    size_mb:   float


@dataclass
class BackupSession:
    """Live state for a prepared restore environment.

    Once ``prepare_backup_session`` returns, the extract pod is running a
    temp postgres on the PVC, ready to be queried for source databases
    and to produce dumps on demand.
    """
    cfg:                dict
    backup:             Backup
    pvc_name:           str
    extract_pod_name:   str
    data_dir:           str
    primary_node:       str
    image:              str
    pg_major:           str
    namespace:          str
    cleanup_artifacts:  list = field(default_factory=list)


class RestoreError(RuntimeError):
    """Operation-level failure with a human-friendly message."""

    def __init__(self, msg: str, detail: str = ""):
        super().__init__(msg)
        self.detail = detail


# ---------------------------------------------------------------------------
# Tiny kubectl/exec helpers
# ---------------------------------------------------------------------------

def _kc_exec(ns: str, pod: str, *cmd: str, container: str = "",
             timeout: int = 120) -> subprocess.CompletedProcess:
    """``kubectl exec -n NS POD [-c C] -- CMD...`` returning text output."""
    args = ["kubectl", "-n", ns, "exec", pod]
    if container:
        args += ["-c", container]
    args += ["--", *cmd]
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout)


def _kc_exec_bash(ns: str, pod: str, script: str, *,
                  container: str = "",
                  timeout: int = 600) -> subprocess.CompletedProcess:
    return _kc_exec(ns, pod, "bash", "-c", script,
                    container=container, timeout=timeout)


def _primary_pod(cfg: dict) -> str:
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", "postgres-operator.crunchydata.com/role=master",
             "-o", "jsonpath={.items[0].metadata.name}")
    pod = (r.stdout or b"").decode().strip()
    if not pod:
        raise RestoreError("Could not find primary pod.")
    return pod


def _primary_pod_spec(cfg: dict) -> dict:
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", "postgres-operator.crunchydata.com/role=master",
             "-o", "json")
    if r.returncode != 0:
        raise RestoreError("Could not get primary pod.")
    items = json.loads(r.stdout.decode()).get("items", [])
    if not items:
        raise RestoreError("No primary pod found.")
    spec = items[0]["spec"]
    image = next((c["image"] for c in spec.get("containers", [])
                  if c["name"] == "database"), None)
    pgbackrest_volume = next((v for v in spec.get("volumes", [])
                              if v["name"] == "pgbackrest-config"), None)
    primary_node = spec.get("nodeName", "")
    if not image or not pgbackrest_volume or not primary_node:
        raise RestoreError(
            "Primary pod is missing image/pgbackrest-config/nodeName.")
    pg_major = "16"
    m = re.search(r"[-:](\d+)\.\d+", image)
    if m:
        pg_major = m.group(1)
    return {"image": image, "pgbackrest_volume": pgbackrest_volume,
            "primary_node": primary_node, "pg_major": pg_major}


def _k8s_name(s: str) -> str:
    return s.replace("_", "-").lower()


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------

def list_backups(cfg: dict, *, reporter: ProgressReporter) -> list[Backup]:
    """Query pgbackrest inside the primary pod and return parsed backups.

    The pgbackrest exec is lightweight; we don't bother with a full
    progress bar -- one step is enough for the user to see the screen
    didn't hang."""
    reporter.set_total(1)
    reporter.step("Querying pgbackrest for available backups...")
    primary = _primary_pod(cfg)
    cmd = ("pgbackrest --config=/etc/pgbackrest.conf "
           "--config-include-path=/etc/pgbackrest/conf.d "
           "--stanza=db info --output=json 2>&1")
    r = _kc_exec_bash(cfg["namespace"], primary, cmd,
                      container="database", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        if "s3-key" in out.lower() or "repo1-s3-key" in out:
            raise RestoreError(
                "pgbackrest is missing S3 credentials. Run "
                "`./manager.py restore` once on the primary node "
                "to register them, then return to the TUI.",
                out.strip())
        raise RestoreError(f"pgbackrest unreachable.", out.strip())
    raw = out.strip()
    if not raw:
        raise RestoreError("pgbackrest returned no output.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RestoreError("pgbackrest returned non-JSON output.",
                           f"{e}\n\n{raw[:300]}") from None
    backups: list[tuple[int, Backup]] = []
    for stanza in data:
        for b in stanza.get("backup", []):
            stop = b["timestamp"]["stop"]
            ts_dt = datetime.datetime.fromtimestamp(
                stop, datetime.timezone.utc)
            backups.append((stop, Backup(
                label=b["label"],
                type=b["type"].upper(),
                timestamp=ts_dt.strftime("%Y-%m-%d %H:%M UTC"),
                date=ts_dt.strftime("%Y%m%d"),
                size_mb=round(b["info"]["size"] / 1024 / 1024, 1),
            )))
    if not backups:
        raise RestoreError(
            "No backups found. Trigger a manual backup first:\n"
            f"  kubectl annotate postgrescluster -n {cfg['namespace']} "
            f"{cfg['cluster']} \\\n"
            "    postgres-operator.crunchydata.com/pgbackrest-backup="
            "\"$(date '+%F_%H:%M:%S')\" --overwrite")
    # Newest first so the cursor lands on the most recent restore point
    # (matches the CLI's "Enter for latest" default and matches every
    # other UI that lists backups). pgbackrest reports them oldest-first.
    backups.sort(key=lambda t: t[0], reverse=True)
    reporter.log(f"found {len(backups)} backup(s)", level="ok")
    return [b for _, b in backups]


# ---------------------------------------------------------------------------
# prepare_backup_session
# ---------------------------------------------------------------------------

# The extract pod runs a stripped-down version of the CLI script: just
# bring up postgres on the PVC and idle. pg_dump is invoked separately
# (per-source-database) so we can drive multiple restores from one
# session without restarting the temp cluster.
_EXTRACT_SCRIPT = """\
set -e
echo "Checking restore data..."
test -f {data_dir}/global/pg_control || {{ echo "ERROR: Restore data not found"; exit 1; }}
echo "Preparing data..."
rm -f {data_dir}/recovery.signal {data_dir}/standby.signal \\
      {data_dir}/backup_label {data_dir}/backup_label.old 2>/dev/null || true
pg_resetwal -f {data_dir}
echo "Starting temp PostgreSQL on 5433..."
postgres -D {data_dir} -p 5433 \\
  -c config_file={data_dir}/postgresql.conf \\
  -c hba_file={data_dir}/pg_hba.conf \\
  -c ident_file={data_dir}/pg_ident.conf \\
  -c listen_addresses=localhost -c unix_socket_directories=/tmp -c ssl=off \\
  -c logging_collector=off -c log_destination=stderr \\
  > /restore-data/postgres.log 2>&1 &
PG_PID=$!
for i in $(seq 30); do
  pg_isready -h /tmp -p 5433 -U postgres >/dev/null 2>&1 && break
  sleep 2
done
pg_isready -h /tmp -p 5433 -U postgres >/dev/null 2>&1 || \\
  {{ kill $PG_PID 2>/dev/null; cat /restore-data/postgres.log; exit 1; }}
echo "TEMP_PG_READY"
tail -f /dev/null
"""


def _check_pvc_has_data(cfg: dict, *, pvc_name: str, primary_node: str,
                       data_dir: str, reporter: ProgressReporter) -> bool:
    """Spin up a tiny busybox check-pod, look for pg_control, return bool."""
    ns = cfg["namespace"]
    name = f"check-{pvc_name[-8:]}-{int(time.time())}"
    overrides = {"spec": {
        "nodeSelector": {"kubernetes.io/hostname": primary_node},
        "containers": [{
            "name":         "check",
            "image":        "busybox",
            "command":      ["sh", "-c",
                             f"test -f {data_dir}/global/pg_control "
                             f"&& echo exists || echo missing"],
            "volumeMounts": [{"name": "data",
                              "mountPath": "/restore-data"}],
        }],
        "volumes": [{"name": "data",
                     "persistentVolumeClaim": {"claimName": pvc_name}}],
    }}
    kube("run", name, "--restart=Never", "--image=busybox", "-n", ns,
         "--overrides=" + json.dumps(overrides))
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(2)
        r = kube("-n", ns, "get", "pod", name,
                 "-o", "jsonpath={.status.phase}")
        if (r.stdout or b"").decode().strip() in ("Succeeded", "Failed"):
            break
    r = kube("-n", ns, "logs", name)
    out = (r.stdout or b"").decode()
    kube("-n", ns, "delete", "pod", name, "--wait=false")
    return "exists" in out


def _restore_from_s3(cfg: dict, *, primary_node: str, image: str,
                     pgbackrest_volume: dict, pvc_name: str,
                     data_dir: str, backup: Backup,
                     reporter: ProgressReporter) -> None:
    ns = cfg["namespace"]
    pod = f"restore-{pvc_name[-12:]}-{int(time.time())}"
    cmd = (
        f"pgbackrest --config=/etc/pgbackrest.conf "
        f"--config-include-path=/etc/pgbackrest/conf.d "
        f"--stanza=db --set={backup.label} restore "
        f"--pg1-path={data_dir} --type=immediate --delta --process-max=4")
    spec = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": ns},
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {"kubernetes.io/hostname": primary_node},
            "containers": [{
                "name":  "restore", "image": image,
                "command": ["/bin/bash", "-c",
                            f"set -e; echo Starting; {cmd}; echo Done"],
                "volumeMounts": [
                    {"name": "pgbackrest-config",
                     "mountPath": "/etc/pgbackrest/conf.d"},
                    {"name": "restore-data",
                     "mountPath": "/restore-data"},
                ],
            }],
            "volumes": [
                pgbackrest_volume,
                {"name": "restore-data",
                 "persistentVolumeClaim": {"claimName": pvc_name}},
            ],
        },
    }
    r = kube("-n", ns, "apply", "-f", "-",
             input_data=yaml.dump(spec).encode())
    if r.returncode != 0:
        raise RestoreError("Failed to create S3-restore pod.",
                           (r.stderr or b"").decode().strip())

    deadline = time.time() + 900
    last_lines: set[str] = set()
    while time.time() < deadline:
        time.sleep(10)
        r = kube("-n", ns, "get", "pod", pod,
                 "-o", "jsonpath={.status.phase}")
        phase = (r.stdout or b"").decode().strip()
        r_logs = kube("-n", ns, "logs", pod, "--tail=5")
        for line in (r_logs.stdout or b"").decode().splitlines():
            if line and line not in last_lines:
                reporter.log(line)
                last_lines.add(line)
        if phase == "Succeeded":
            kube("-n", ns, "delete", "pod", pod, "--wait=false")
            return
        if phase == "Failed":
            r2 = kube("-n", ns, "logs", pod, "--tail=50")
            kube("-n", ns, "delete", "pod", pod, "--wait=false")
            raise RestoreError("S3 restore pod failed.",
                               (r2.stdout or b"").decode().strip())
    kube("-n", ns, "delete", "pod", pod, "--wait=false")
    raise RestoreError("S3 restore did not complete within 15 minutes.")


def _ensure_extract_pod(cfg: dict, *, pod_name: str, primary_node: str,
                        image: str, pvc_name: str, data_dir: str,
                        reporter: ProgressReporter) -> None:
    """Create the extract pod, or reuse it if it's already healthy.

    Idempotent: any pre-existing pod that's not Running with the
    "TEMP_PG_READY" sentinel is deleted and re-created. PVC bindings
    survive across deletions, so this is safe."""
    ns = cfg["namespace"]
    r = kube("-n", ns, "get", "pod", pod_name,
             "-o", "jsonpath={.status.phase}")
    phase = (r.stdout or b"").decode().strip() if r.returncode == 0 else ""

    reuse = False
    if phase == "Running":
        r_logs = kube("-n", ns, "logs", pod_name, "--tail=20")
        if "TEMP_PG_READY" in (r_logs.stdout or b"").decode():
            reuse = True
    if not reuse and phase:
        reporter.log(f"removing stale extract pod (phase={phase})",
                     level="warn")
        kube("-n", ns, "delete", "pod", pod_name,
             "--wait=true", "--timeout=60s")

    if reuse:
        reporter.log(f"reusing existing extract pod {pod_name}", level="ok")
        return

    spec = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": ns},
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {"kubernetes.io/hostname": primary_node},
            "containers": [{
                "name":     "postgres",
                "image":    image,
                "command":  ["/bin/bash", "-c",
                             _EXTRACT_SCRIPT.format(data_dir=data_dir)],
                "volumeMounts": [{"name": "restore-data",
                                  "mountPath": "/restore-data"}],
                "resources": {
                    "requests": {"memory": "2Gi", "cpu": "1000m"},
                    "limits":   {"memory": "4Gi", "cpu": "2000m"},
                },
            }],
            "volumes": [{"name": "restore-data",
                         "persistentVolumeClaim": {"claimName": pvc_name}}],
        },
    }
    r = kube("-n", ns, "apply", "-f", "-",
             input_data=yaml.dump(spec).encode())
    if r.returncode != 0:
        raise RestoreError("Failed to create extract pod.",
                           (r.stderr or b"").decode().strip())

    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(5)
        r = kube("-n", ns, "logs", pod_name, "--tail=10")
        out = (r.stdout or b"").decode()
        if "TEMP_PG_READY" in out:
            reporter.log("temp postgres is ready", level="ok")
            return
        if "ERROR:" in out:
            raise RestoreError("Extract pod reported an error.", out.strip())
    raise RestoreError("Extract pod did not become ready within 10 minutes.")


def prepare_backup_session(cfg: dict, *, backup: Backup,
                           reporter: ProgressReporter,
                           force_fresh: bool = False) -> BackupSession:
    """Materialise PVC + S3 restore (if needed) + extract pod.

    Returns a BackupSession that subsequent calls (list_databases_in_backup,
    restore_database) can reuse without re-doing the heavy steps.

    ``force_fresh=True`` deletes any existing PVC and extract pod for this
    backup date first, forcing a clean re-download from S3. Useful when
    the user suspects the on-PVC data is stale or corrupt, or when the
    backup was overwritten on S3 since the last local mount.
    """
    reporter.set_total(5)

    reporter.step("Resolving primary pod and image...")
    pod_spec = _primary_pod_spec(cfg)
    primary_node      = pod_spec["primary_node"]
    image             = pod_spec["image"]
    pgbackrest_volume = pod_spec["pgbackrest_volume"]
    pg_major          = pod_spec["pg_major"]
    reporter.log(f"primary node = {primary_node}", level="ok")
    reporter.log(f"image        = {image}", level="ok")

    friendly = datetime.datetime.strptime(
        backup.date, "%Y%m%d").strftime("%b%d").lower()
    data_dir         = f"/restore-data/pg{pg_major}-{friendly}"
    pvc_name         = f"restore-pvc-{backup.date}"
    extract_pod_name = f"restore-extract-{backup.date}"
    ns               = cfg["namespace"]

    if force_fresh:
        reporter.log("force_fresh=True - wiping any existing artifacts",
                     level="warn")
        # Order matters: the extract pod has the PVC mounted, so we need
        # to delete the pod (and wait for it to detach) before the PVC
        # will release.
        kube("-n", ns, "delete", "pod", extract_pod_name,
             "--ignore-not-found", "--wait=true", "--timeout=60s")
        reporter.log(f"deleted extract pod {extract_pod_name}", level="ok")
        kube("-n", ns, "delete", "pvc", pvc_name,
             "--ignore-not-found", "--wait=true", "--timeout=120s")
        reporter.log(f"deleted PVC {pvc_name}", level="ok")

    reporter.step("Ensuring PersistentVolumeClaim...")
    r = kube("-n", ns, "get", "pvc", pvc_name, "-o", "name")
    pvc_exists = r.returncode == 0 and pvc_name in (r.stdout or b"").decode()
    if pvc_exists:
        reporter.log(f"PVC {pvc_name} already exists", level="ok")
    else:
        manifest = {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes":  ["ReadWriteOnce"],
                "resources":    {"requests": {"storage": "20Gi"}},
            },
        }
        r = kube("-n", ns, "apply", "-f", "-",
                 input_data=yaml.dump(manifest).encode())
        if r.returncode != 0:
            raise RestoreError("Failed to create PVC.",
                               (r.stderr or b"").decode().strip())
        reporter.log(f"PVC {pvc_name} created", level="ok")

    reporter.step("Checking whether the cluster files are already on PVC...")
    have_data = _check_pvc_has_data(
        cfg, pvc_name=pvc_name, primary_node=primary_node,
        data_dir=data_dir, reporter=reporter)
    if have_data:
        reporter.log("cluster files already present", level="ok")

    if not have_data:
        reporter.step("Restoring from S3 (10-15 minutes)...")
        _restore_from_s3(
            cfg, primary_node=primary_node, image=image,
            pgbackrest_volume=pgbackrest_volume, pvc_name=pvc_name,
            data_dir=data_dir, backup=backup, reporter=reporter)
    else:
        reporter.step("Skipping S3 restore (data already on PVC)",
                      advance=True)

    reporter.step("Bringing up extract pod with temp PostgreSQL...")
    _ensure_extract_pod(
        cfg, pod_name=extract_pod_name, primary_node=primary_node,
        image=image, pvc_name=pvc_name, data_dir=data_dir,
        reporter=reporter)

    return BackupSession(
        cfg=cfg, backup=backup,
        pvc_name=pvc_name,
        extract_pod_name=extract_pod_name,
        data_dir=data_dir,
        primary_node=primary_node,
        image=image,
        pg_major=pg_major,
        namespace=ns,
    )


# ---------------------------------------------------------------------------
# list_databases_in_backup
# ---------------------------------------------------------------------------

def list_databases_in_backup(session: BackupSession, *,
                             reporter: ProgressReporter) -> list[dict]:
    """Query the temp postgres in the extract pod for non-template DBs.

    Returns ``[{name, owner, size}, ...]`` in the same shape as
    fetch_databases() so screens can reuse the same column rendering."""
    reporter.set_total(1)
    reporter.step("Listing databases in the backup...")
    sql = (
        "SELECT datname, pg_get_userbyid(datdba), "
        "pg_size_pretty(pg_database_size(datname)) "
        "FROM pg_database WHERE datistemplate = false ORDER BY datname;")
    r = _kc_exec(session.namespace, session.extract_pod_name,
                 "psql", "-h", "/tmp", "-p", "5433", "-U", "postgres",
                 "-d", "postgres", "-t", "-A", "-c", sql,
                 timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RestoreError("Could not list databases in the backup.",
                           out.strip())
    rows: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            rows.append({"name":  parts[0],
                         "owner": parts[1],
                         "size":  parts[2]})
    reporter.log(f"backup contains {len(rows)} database(s)", level="ok")
    return rows


# ---------------------------------------------------------------------------
# restore_database
# ---------------------------------------------------------------------------

def list_session_artifacts(cfg: dict) -> list[dict]:
    """Return all currently-existing TUI restore artifacts.

    Each entry: ``{date, pvc, pvc_capacity, pvc_phase, pod, pod_phase}``.
    Used by the cleanup UI so the user knows what they'd be freeing.
    """
    ns = cfg["namespace"]
    artifacts: dict[str, dict] = {}

    r = kube("-n", ns, "get", "pvc",
             "-l", "", "-o", "json")  # no label, list all
    if r.returncode == 0:
        for item in json.loads(r.stdout or b"{}").get("items", []):
            name = item["metadata"]["name"]
            if not name.startswith("restore-pvc-"):
                continue
            date = name[len("restore-pvc-"):]
            cap  = item.get("spec", {}).get("resources", {}) \
                       .get("requests", {}).get("storage", "?")
            phase = item.get("status", {}).get("phase", "?")
            artifacts.setdefault(date, {"date": date, "pvc": "", "pod": ""})
            artifacts[date].update(pvc=name, pvc_capacity=cap,
                                   pvc_phase=phase)

    r = kube("-n", ns, "get", "pod", "-o", "json")
    if r.returncode == 0:
        for item in json.loads(r.stdout or b"{}").get("items", []):
            name = item["metadata"]["name"]
            if not name.startswith("restore-extract-"):
                continue
            date = name[len("restore-extract-"):]
            phase = item.get("status", {}).get("phase", "?")
            artifacts.setdefault(date, {"date": date, "pvc": "", "pod": ""})
            artifacts[date].update(pod=name, pod_phase=phase)

    out = list(artifacts.values())
    out.sort(key=lambda a: a["date"], reverse=True)
    return out


def cleanup_session(cfg: dict, *, backup_date: str,
                    reporter: ProgressReporter) -> OperationResult:
    """Delete the PVC + extract pod for a given backup date.

    Frees the 20Gi PVC (the dominant cost) plus the small idle extract
    pod. Idempotent: missing artifacts are ignored.
    """
    reporter.set_total(2)
    ns               = cfg["namespace"]
    pvc_name         = f"restore-pvc-{backup_date}"
    extract_pod_name = f"restore-extract-{backup_date}"

    # Pod first (otherwise the PVC stays bound until the pod's gone).
    reporter.step(f"Deleting extract pod {extract_pod_name}...")
    r = kube("-n", ns, "delete", "pod", extract_pod_name,
             "--ignore-not-found", "--wait=true", "--timeout=60s")
    pod_msg = (r.stdout or b"").decode().strip() \
            or (r.stderr or b"").decode().strip()
    reporter.log(pod_msg or "(nothing to delete)",
                 level="ok" if r.returncode == 0 else "warn")

    reporter.step(f"Deleting PVC {pvc_name}...")
    r = kube("-n", ns, "delete", "pvc", pvc_name,
             "--ignore-not-found", "--wait=true", "--timeout=120s")
    pvc_msg = (r.stdout or b"").decode().strip() \
            or (r.stderr or b"").decode().strip()
    reporter.log(pvc_msg or "(nothing to delete)",
                 level="ok" if r.returncode == 0 else "warn")

    if r.returncode != 0:
        return OperationResult(False,
            f"Could not fully clean up backup {backup_date}.",
            detail=(pod_msg + "\n" + pvc_msg).strip())
    return OperationResult(True,
        f"Removed restore artifacts for {backup_date}.",
        detail=(pod_msg + "\n" + pvc_msg).strip())


def restore_database(*, session: BackupSession,
                     source_db: str, target_db: str,
                     reporter: ProgressReporter) -> OperationResult:
    """pg_dump in the extract pod -> kubectl cp -> pg_restore on primary."""
    reporter.set_total(7)
    cfg = session.cfg
    ns  = session.namespace
    pod = session.extract_pod_name

    # ---- 1. Pre-flight: target name not taken --------------------------
    reporter.step("Checking target database name is available...")
    primary = _primary_pod(cfg)
    safe_target = target_db.replace("'", "''")
    r = _kc_exec(ns, primary, "psql", "-U", "postgres",
                 "-d", "postgres", "-t", "-A", "-c",
                 f"SELECT 1 FROM pg_database WHERE datname='{safe_target}';",
                 container="database", timeout=30)
    if (r.stdout or "").strip() == "1":
        return OperationResult(False,
            f"Target database '{target_db}' already exists.")
    reporter.log("target name is free", level="ok")

    # ---- 2. pg_dump from extract pod's temp postgres ------------------
    reporter.step(f"Dumping '{source_db}' from backup...")
    dump_path = f"/restore-data/{source_db}-{int(time.time())}.dump"
    safe_source = source_db.replace('"', '""')
    dump_cmd = (
        f"pg_dump -h /tmp -p 5433 -U postgres -Fc "
        f"--exclude-schema=pgbouncer "
        f'-d "{safe_source}" -f {dump_path}')
    r = _kc_exec_bash(ns, pod, dump_cmd, timeout=1200)
    if r.returncode != 0:
        return OperationResult(False, f"pg_dump failed: {source_db}",
                               (r.stdout or "") + (r.stderr or ""))
    reporter.log(f"dump written to {dump_path}", level="ok")

    # ---- 3. kubectl cp dump out of extract pod ------------------------
    reporter.step("Copying dump out of extract pod...")
    local_dump = os.path.join(
        os.environ.get("TMPDIR", "/tmp"),
        f"pgrestore-{cfg['cluster']}-{int(time.time())}.dump")
    r = subprocess.run(
        ["kubectl", "cp", f"{ns}/{pod}:{dump_path}", local_dump],
        capture_output=True, text=True, timeout=1200)
    if r.returncode != 0:
        return OperationResult(False, "kubectl cp (out) failed.",
                               r.stderr.strip())
    size_mb = os.path.getsize(local_dump) / 1024 / 1024
    reporter.log(f"dump size = {size_mb:.1f} MB", level="ok")

    try:
        # ---- 4. CREATE DATABASE on primary ---------------------------
        reporter.step(f"CREATE DATABASE \"{target_db}\" on primary...")
        safe_q = target_db.replace('"', '""')
        r = _kc_exec(ns, primary, "psql", "-U", "postgres", "-d", "postgres",
                     "-c", f'CREATE DATABASE "{safe_q}";',
                     container="database", timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or "ERROR" in out.upper():
            return OperationResult(False, "CREATE DATABASE failed.",
                                   out.strip())
        admin = cfg.get("admin_user", "")
        if admin:
            _kc_exec(ns, primary, "psql", "-U", "postgres", "-d", "postgres",
                     "-c", f'ALTER DATABASE "{safe_q}" OWNER TO "{admin}";',
                     container="database", timeout=30)
        reporter.log("database created", level="ok")

        # ---- 5. Drop the inherited pgbouncer schema ------------------
        # Belt-and-suspenders: pg_dump already strips it, but template1
        # carries the schema in PGO clusters and CREATE DATABASE inherits
        # it; without this drop, pg_restore fails on "schema already
        # exists" the moment any object references it.
        reporter.step("Dropping inherited pgbouncer schema...",
                      advance=True)
        r = _kc_exec(ns, primary, "psql", "-U", "postgres",
                     "-d", target_db, "-c",
                     "DROP SCHEMA IF EXISTS pgbouncer CASCADE;",
                     container="database", timeout=30)
        out = (r.stdout or "") + (r.stderr or "")
        if "ERROR" in out.upper() and "does not exist" not in out.lower():
            reporter.log(f"warning: {out.strip()}", level="warn")
        else:
            reporter.log("pgbouncer schema cleared", level="ok")

        # ---- 6. kubectl cp dump into primary -------------------------
        reporter.step("Copying dump into primary pod...")
        r = subprocess.run(
            ["kubectl", "cp", local_dump,
             f"{ns}/{primary}:/tmp/restore.dump", "-c", "database"],
            capture_output=True, text=True, timeout=1200)
        if r.returncode != 0:
            return OperationResult(False, "kubectl cp (in) failed.",
                                   r.stderr.strip())
        reporter.log("dump in place on primary", level="ok")

        # ---- 7. pg_restore on primary --------------------------------
        reporter.step(f"pg_restore into '{target_db}'...")
        r = _kc_exec(ns, primary, "pg_restore", "-U", "postgres",
                     "-d", target_db, "-v", "/tmp/restore.dump",
                     container="database", timeout=3600)
        # pg_restore prints progress on stderr even on success; check
        # the exit code first, but flag genuine ERRORs in the body.
        body = ((r.stdout or "") + (r.stderr or "")).strip()
        # Cleanup the dump from the primary regardless.
        _kc_exec(ns, primary, "rm", "-f", "/tmp/restore.dump",
                 container="database", timeout=30)
        if r.returncode != 0:
            return OperationResult(False,
                f"pg_restore failed (exit {r.returncode}).", body)

        # Verify by counting public tables.
        r = _kc_exec(ns, primary, "psql", "-U", "postgres",
                     "-d", target_db, "-t", "-A", "-c",
                     "SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema='public';",
                     container="database", timeout=30)
        tcount = (r.stdout or "").strip() or "0"
        reporter.log(f"public tables in target = {tcount}", level="ok")

        return OperationResult(
            True,
            f"Restored '{source_db}' as '{target_db}' "
            f"({tcount} public tables).",
            detail=body,
            data={"source_db": source_db, "target_db": target_db,
                  "table_count": tcount},
        )
    finally:
        try:
            if os.path.exists(local_dump):
                os.remove(local_dump)
        except OSError:
            pass
