# create-db.sh vs manager.py create

## Side-by-side comparison

| Aspect | create-db.sh | manager.py create |
|--------|--------------|-------------------|
| **Input** | Single arg: `<database-name>`. Username derived from DB name (lowercase, hyphens, alphanumeric; K8s-safe). | Prompts (or flags): database name, username, password (optional = auto-generate). |
| **Password** | PGO generates it. Script waits for secret and reads password after. | You set it (or auto-gen). Manager creates the Secret with that password *before* updating spec so PGO adopts it. |
| **Spec update** | `kubectl patch` — type=json, replace `/spec/users` with merged array. No YAML file. | Fetches cluster spec as YAML, writes to `YAML` path, appends/updates users, `kubectl apply -f`. Requires `YAML` in .env. |
| **Existence check** | DB: from cluster spec (`.spec.users[].databases`). User: from `.spec.users[].name`; if user exists, append random suffix. | DB: `SELECT` in Postgres. No check for existing username in spec (can conflict if user already in spec). |
| **Wait** | Primary pod → poll `pg_database` until DB exists (max 120s) → sleep 5 → get secret (wait up to 30s). | Wait for secret `.data.verifier` (PGO reconciliation), up to ~2 min. |
| **Grants / ownership** | Full: `GRANT ALL ON SCHEMA public`; `GRANT ALL PRIVILEGES ON DATABASE`; `ALTER SCHEMA public OWNER TO user`; `ALTER DATABASE OWNER TO user`; `ALTER DEFAULT PRIVILEGES` (tables, sequences). | Only: `GRANT ALL PRIVILEGES ON DATABASE`; `ALTER DATABASE OWNER`. *(Manager was missing schema and default privileges; now aligned—see below.)* |
| **Config** | Hardcoded defaults (NAMESPACE, CLUSTER, EXTERNAL_HOST). | From `pg-db-manager.env` (portable, per cluster). |
| **Dependencies** | bash, kubectl, jq, openssl. | Python 3, PyYAML, kubectl. |

## Alignments made in manager

- **Grants** — Manager now runs the same privilege set as create-db.sh: grant on schema public, alter schema owner, alter database owner, and default privileges for tables/sequences, so apps can create/alter/drop tables without extra setup.

## Optional improvements (manager)

- **Patch instead of YAML** — Add a code path that uses `kubectl patch --type=json` to add the user (like create-db.sh) so create works even when `YAML` is not set or cluster is not backed by a local YAML file.
- **Username from DB name** — If `--user` is omitted, derive a K8s-safe username from the database name (e.g. `my_new_db` → `my-new-db`) and optionally uniquify if that user already exists in the spec.
- **Check spec for existing user** — Before creating secret/spec, read `.spec.users` and if the chosen username already exists, either fail with a clear message or append a suffix (like create-db.sh).
