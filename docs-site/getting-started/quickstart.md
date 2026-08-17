# Quickstart

The fastest way to see everything work together — real Postgres, real
MinIO, real MLflow, real Airflow, real XGBoost:

```bash
git clone <this-repo> && cd Framework
cp .env.example .env.docker

docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker run --rm app alembic upgrade head

# Full governance chain on a synthetic dataset, driven entirely by the
# framework (dataset → readiness → eligibility → real Airflow DAG →
# promotion → serving reload → lineage):
docker compose --env-file .env.docker --profile demo run --rm demo
```

Then open:

| URL | What you'll see |
|---|---|
| <http://localhost:8000> | **Gateflow** — dashboard, the promoted model, its metrics, its lineage graph |
| <http://localhost:5000> | MLflow — the real training run: params, metrics, logged artifact |
| <http://localhost:8080> | Airflow — the DAG run that actually executed the training (`airflow`/`airflow`) |
| <http://localhost:9001> | MinIO console — the uploaded model artifact (`minioadmin`/`minioadmin`) |

No Docker? See [Installation](installation.md) for a SQLite-only path
with no external services.

## Where to go next

- Want to see the *whole* lifecycle, including drift detection and a
  human approval gate? Read the [Closed-Loop Demo](../demos/closed-loop-demo.md).
- Want to write application code against the framework? Start with
  [Using the SDK](../sdk/using-the-sdk.md).
- Want to understand what's actually running behind Gateflow? Read
  [Architecture](../concepts/architecture.md).
