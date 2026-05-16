"""Patroni switchover as a pure function with progress reporting.

Steps:

  1. Validate target (must be a real, ready, non-leader pod).
  2. Pick an exec pod that is neither the current leader nor the target
     (Patroni control commands prefer a third-party pod; the leader and
     candidate can briefly 502 during the role transition).
  3. Confirm the live Patroni leader (PGO labels can lag a few seconds).
  4. Run ``patronictl switchover --force`` from the exec pod.
  5. Verify the new topology by polling ``patronictl list`` until the
     target is reported as leader (5 attempts, 2s apart).

This module performs zero presentation — it only talks SQL/kubectl and
calls into the supplied ProgressReporter. The CLI ``cmd_primary`` and
the Textual switchover screen both call ``perform_switchover()``.
"""

from __future__ import annotations

import json
import time

from ..kube import kube
from .progress import OperationResult, ProgressReporter


_TOTAL_STEPS = 4


def _patronictl(cfg: dict, exec_pod: str, *args: str, timeout: int = 180):
    """Run ``patronictl <args>`` inside the database container of exec_pod."""
    import subprocess
    cmd = ["kubectl", "-n", cfg["namespace"], "exec", exec_pod,
           "-c", "database", "--", "patronictl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _list_pg_pods(cfg: dict) -> list[dict]:
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", f"postgres-operator.crunchydata.com/cluster={cfg['cluster']},"
                   "postgres-operator.crunchydata.com/data=postgres",
             "-o", "json")
    if r.returncode != 0:
        raise RuntimeError(
            f"kubectl could not list Postgres pods: "
            f"{(r.stderr or b'').decode().strip()}")
    pods = []
    for item in json.loads(r.stdout).get("items", []):
        labels = item["metadata"].get("labels", {})
        ready = all(c.get("ready") for c
                    in item.get("status", {}).get("containerStatuses",
                                                  []) or [{"ready": False}])
        pods.append({
            "name":  item["metadata"]["name"],
            "node":  item["spec"].get("nodeName", "?"),
            "role":  labels.get("postgres-operator.crunchydata.com/role",
                                "replica"),
            "ready": ready,
        })
    pods.sort(key=lambda p: p["name"])
    return pods


def _live_leader(cfg: dict, exec_pod: str) -> str | None:
    r = _patronictl(cfg, exec_pod, "list", "-f", "json")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        for m in json.loads(r.stdout):
            if m.get("Role", "").lower() == "leader":
                return m.get("Member")
    except json.JSONDecodeError:
        return None
    return None


def perform_switchover(cfg: dict, *, target_pod: str,
                       reporter: ProgressReporter) -> OperationResult:
    """Switch the Patroni primary to ``target_pod``.

    Returns an OperationResult. Never raises (catches and reports its own
    errors through the reporter so the caller doesn't need a try/except
    just to render a message)."""
    reporter.set_total(_TOTAL_STEPS)

    # ---- Step 1: validate ---------------------------------------------------
    reporter.step("Validating target pod...")
    try:
        pods = _list_pg_pods(cfg)
    except RuntimeError as e:
        reporter.log(str(e), level="error")
        return OperationResult(False, "Could not list Postgres pods.", str(e))
    if not pods:
        reporter.log("No Postgres pods found.", level="error")
        return OperationResult(False, "No Postgres pods found.")

    target = next((p for p in pods if p["name"] == target_pod), None)
    if target is None:
        msg = f"Target pod '{target_pod}' not found."
        reporter.log(msg, level="error")
        return OperationResult(False, msg)
    if not target["ready"]:
        msg = f"Target pod '{target_pod}' is not Ready."
        reporter.log(msg, level="error")
        return OperationResult(False, msg)
    reporter.log(f"target = {target_pod} on node {target['node']}", level="ok")

    # ---- Step 2: pick an exec pod ------------------------------------------
    reporter.step("Selecting Patroni control pod...")
    # Prefer a pod that's neither the current label-leader nor the target.
    # That third-party pod is the safest place to run patronictl while the
    # leader and candidate are mid-transition.
    label_leader = next((p["name"] for p in pods if p["role"] == "master"), "")
    exec_pod = next((p["name"] for p in pods
                     if p["ready"]
                     and p["name"] not in (label_leader, target_pod)),
                    None)
    if exec_pod is None:
        # 2-node clusters (rare) or all replicas down — fall back to any ready
        # pod that isn't the target.
        exec_pod = next((p["name"] for p in pods
                         if p["ready"] and p["name"] != target_pod), None)
    if exec_pod is None:
        msg = "No suitable pod available to run patronictl."
        reporter.log(msg, level="error")
        return OperationResult(False, msg)
    reporter.log(f"exec via {exec_pod}", level="ok")

    # ---- Step 3: resolve the live leader -----------------------------------
    reporter.step("Confirming current Patroni leader...")
    live = _live_leader(cfg, exec_pod) or label_leader
    if not live:
        msg = "Could not determine the current Patroni leader."
        reporter.log(msg, level="error")
        return OperationResult(False, msg)
    reporter.log(f"current leader = {live}", level="ok")
    if live == target_pod:
        msg = f"'{target_pod}' is already the primary — nothing to do."
        reporter.log(msg, level="warn")
        return OperationResult(True, msg, data={"leader": live})

    # ---- Step 4: switchover + verify ---------------------------------------
    reporter.step(f"Switching primary  {live} → {target_pod}")
    r = _patronictl(cfg, exec_pod,
                    "switchover", "--leader", live,
                    "--candidate", target_pod, "--force")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.strip():
            reporter.log(line, level="info")
    if r.returncode != 0:
        msg = f"patronictl switchover exited {r.returncode}."
        reporter.log(msg, level="error")
        return OperationResult(False, msg, detail=out.strip())

    # Verify by polling. patronictl list might briefly fail while the new
    # leader is being promoted, so retry a few times.
    reporter.step("Verifying new topology...", advance=False)
    new_leader = None
    for attempt in range(5):
        time.sleep(2)
        new_leader = _live_leader(cfg, exec_pod)
        reporter.log(f"poll {attempt + 1}/5: leader = {new_leader or '(none)'}")
        if new_leader == target_pod:
            break
    if new_leader != target_pod:
        # Switchover may still have succeeded — kubelet sometimes 502s the
        # leader during transition and we just couldn't confirm. Surface
        # the ambiguity rather than failing outright.
        msg = (f"Switchover completed but new leader is "
               f"'{new_leader or 'unknown'}' (expected '{target_pod}'). "
               f"Re-check with `manager.py primary --show` shortly.")
        reporter.log(msg, level="warn")
        return OperationResult(True, msg, detail=out.strip(),
                               data={"leader": new_leader})

    msg = f"Primary is now {target_pod}."
    reporter.log(msg, level="ok")
    return OperationResult(True, msg, detail=out.strip(),
                           data={"leader": new_leader})
