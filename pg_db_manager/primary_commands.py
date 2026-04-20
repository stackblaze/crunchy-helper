"""Primary management: show cluster topology and trigger Patroni switchover.

Lets the operator pick which node/pod should hold the primary role. Uses
`patronictl switchover` inside the Patroni database container — works from
any machine with kubectl access (no need to be on the primary node, unlike
the rest of the tool).
"""

import json
import subprocess

from .config import die, divider, info, ok, warn
from .kube import kube


def _list_postgres_pods(cfg: dict) -> list:
    """Return [{name, node, role, instance_set, ready}, ...] for every PG pod."""
    r = kube("-n", cfg["namespace"], "get", "pod",
             "-l", f"postgres-operator.crunchydata.com/cluster={cfg['cluster']},"
                   "postgres-operator.crunchydata.com/data=postgres",
             "-o", "json")
    if r.returncode != 0:
        die(f"Could not list Postgres pods: {r.stderr.decode().strip()}")
    pods = []
    for item in json.loads(r.stdout).get("items", []):
        labels = item["metadata"].get("labels", {})
        ready = all(c.get("ready") for c in item.get("status", {}).get("containerStatuses", []) or [{"ready": False}])
        pods.append({
            "name":         item["metadata"]["name"],
            "node":         item["spec"].get("nodeName", "?"),
            "role":         labels.get("postgres-operator.crunchydata.com/role", "replica"),
            "instance_set": labels.get("postgres-operator.crunchydata.com/instance-set", ""),
            "ready":        ready,
        })
    pods.sort(key=lambda p: p["name"])
    return pods


def _pick_exec_pod(pods: list) -> str:
    """Pick any running pod to exec patronictl in. Prefer a non-leader for safety."""
    for p in pods:
        if p["ready"] and p["role"] != "master":
            return p["name"]
    for p in pods:
        if p["ready"]:
            return p["name"]
    die("No ready Postgres pods found.")


def _patronictl(cfg: dict, exec_pod: str, *args, capture: bool = True):
    """Run `patronictl <args>` inside the database container of exec_pod."""
    cmd = ["kubectl", "-n", cfg["namespace"], "exec", exec_pod, "-c", "database",
           "--", "patronictl", *args]
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return subprocess.run(cmd, text=True, timeout=180)


def _print_topology(pods: list, current_leader: str):
    print(f"\n  Current primary: {current_leader or '(unknown)'}\n")
    print(f"  {'#':>3}  {'POD':<32} {'NODE':<22} {'ROLE':<10} READY")
    print(f"  {'---':>3}  {'-' * 32} {'-' * 22} {'-' * 10} -----")
    for i, p in enumerate(pods):
        marker = "  <-- leader" if p["name"] == current_leader else ""
        role = "leader" if p["name"] == current_leader else "replica"
        print(f"  {i + 1:>3}) {p['name']:<32} {p['node']:<22} {role:<10} "
              f"{'yes' if p['ready'] else 'NO '}{marker}")
    print()


def _resolve_target(target: str, pods: list) -> dict:
    """Map a user-supplied target (number, pod name, or node name) to a pod dict."""
    if target.isdigit():
        idx = int(target) - 1
        if not (0 <= idx < len(pods)):
            die(f"Invalid number: {target}")
        return pods[idx]
    matches_pod = [p for p in pods if p["name"] == target]
    if matches_pod:
        return matches_pod[0]
    matches_node = [p for p in pods if p["node"] == target]
    if len(matches_node) == 1:
        return matches_node[0]
    if len(matches_node) > 1:
        die(f"Multiple pods on node '{target}': "
            + ", ".join(p["name"] for p in matches_node))
    die(f"No pod or node matches '{target}'.")


def cmd_primary(cfg: dict, args):
    """List/switch Patroni primary. Subcommand-less form: interactive."""
    pods = _list_postgres_pods(cfg)
    if not pods:
        die("No Postgres pods found in cluster.")

    current_leader = next((p["name"] for p in pods if p["role"] == "master"), "")
    exec_pod = _pick_exec_pod(pods)

    # Refresh leader from Patroni itself (label can lag a few seconds during a switch).
    r = _patronictl(cfg, exec_pod, "list", "-f", "json")
    if r.returncode == 0 and r.stdout.strip():
        try:
            for m in json.loads(r.stdout):
                if m.get("Role", "").lower() == "leader":
                    current_leader = m.get("Member", current_leader)
                    break
        except json.JSONDecodeError:
            pass

    _print_topology(pods, current_leader)

    if getattr(args, "show", False):
        return

    target_arg = getattr(args, "to", None)
    if not target_arg:
        sel = input("  Promote which? (number, pod name, or node name; Enter to skip): ").strip()
        if not sel:
            print()
            return
        target_arg = sel

    target = _resolve_target(target_arg, pods)

    if target["name"] == current_leader:
        ok(f"'{target['name']}' is already the primary — nothing to do.\n")
        return
    if not target["ready"]:
        die(f"Target pod '{target['name']}' is not ready. Cannot promote.")

    print()
    divider()
    print(f"  SWITCHOVER")
    divider()
    print(f"  From : {current_leader}")
    print(f"  To   : {target['name']}  (node: {target['node']})")
    divider()
    print()

    if not getattr(args, "yes", False):
        confirm = input(f"  Type  {target['name']}  to confirm: ").strip()
        print()
        if confirm != target["name"]:
            print("  [ABORT] Names do not match — no switchover performed.\n")
            return

    info("Triggering Patroni switchover...")
    r = _patronictl(cfg, exec_pod,
                    "switchover", "--leader", current_leader,
                    "--candidate", target["name"], "--force")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        die(f"switchover failed (exit {r.returncode}):\n{out.strip()}")
    if "successfully switched over" not in out.lower() and "switchover" not in out.lower():
        warn(f"switchover output was unexpected:\n{out.strip()}")

    ok("Switchover completed.")
    print()
    info("New cluster state:")
    # Avoid execing into the freshly-promoted pod and the just-demoted old leader;
    # both can briefly 502 on kubelet during role transitions. Prefer a third pod.
    verify_pod = next((p["name"] for p in pods
                       if p["ready"]
                       and p["name"] not in (target["name"], current_leader)),
                      target["name"])
    import time as _time
    for attempt in range(5):
        r2 = _patronictl(cfg, verify_pod, "list")
        if r2.returncode == 0 and r2.stdout.strip():
            print(r2.stdout)
            return
        _time.sleep(2)
    warn("Could not fetch post-switchover topology (cluster may still be settling). "
         "Run `./manager.py primary --show` in a few seconds.")
