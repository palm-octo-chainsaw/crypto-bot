# Kubernetes deployment

The bot runs as a single-replica Deployment in the `default` namespace of the
k3s cluster, using the in-cluster PostgreSQL (`postgres:5432`) for persistence.

## Manifests
- `pvc.yaml` — persistent volumes for `config/targets.json` and the TRW
  Playwright session (`local-path` storage).
- `secret.example.yaml` — template for `crypto-bot-secret` (Opaque, consumed
  via `envFrom`). **Never commit real values.**
- `deployment.yaml` — the bot Deployment (`replicas: 1`, `strategy: Recreate`,
  no Service — the bot is outbound-only).

## Why replicas: 1 + Recreate
Only one process may poll the Telegram bot token at a time; two would cause
`getUpdates` 409 conflicts. `Recreate` guarantees the old pod is gone before
the new one starts during a rollout.

## One-time cutover (SQLite → Postgres, compose → k8s)

Run on the cluster host. The DB reuses the postgres superuser against a new
`cryptobot` database.

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K="sudo -n k3s kubectl -n default"

# 0. Build a Postgres-enabled image WITHOUT deploying. The deploy job only runs
#    on tag pushes, so trigger the build via workflow_dispatch (this tags both
#    :<version> and :latest). A tag push here would fail because the Deployment
#    doesn't exist yet.
#      gh workflow run docker-publish.yml -f version=v1.7.0
#    Wait for it to finish before continuing.

# 1. Create the dedicated database (superuser creds from postgres-secret).
$K exec postgres-0 -- psql -U <superuser> -c "CREATE DATABASE cryptobot;"

# 2. Create the runtime secret (DATABASE_URL points at the new db on postgres:5432).
$K create secret generic crypto-bot-secret \
  --from-env-file=/path/to/.env \
  --from-literal=DATABASE_URL="postgresql://<user>:<pass>@postgres:5432/cryptobot"

# 3. Create the volumes.
$K apply -f k8s/pvc.yaml

# 4. Migrate existing data (schema is created by the script). Run with the
#    cleaned portfolio.db and DATABASE_URL pointing at cryptobot.
DATABASE_URL="postgresql://<user>:<pass>@<host>:5432/cryptobot" \
  python scripts/migrate_sqlite_to_pg.py /path/to/portfolio.db

# 5. Deploy the bot.
$K apply -f k8s/deployment.yaml

# 6. Seed real on-disk state into the PVCs (so we keep current targets and the
#    TRW login). Copy into the running pod's mounted volumes:
POD=$($K get pod -l app=crypto-bot -o jsonpath='{.items[0].metadata.name}')
$K cp config/targets.json "$POD":/app/config/targets.json
$K cp .trw_session/state.json "$POD":/app/.trw_session/state.json

# 7. Stop the old compose bot LAST — only one poller may run at a time.
cd ~/crypto-bot && docker compose down

# 8. Verify: bot online in Telegram, /status, /performance (history intact),
#    /info shows the right version.
$K logs -l app=crypto-bot --tail=40
```

## Ongoing deploys
A `v*.*.*` tag push builds the image and the CI `deploy` job runs
`kubectl set image deployment/crypto-bot bot=…:<version>` + `rollout status`.
