# REST API Reference

65 REST endpoints under `/api`, plus the two probes at the root
(`/health`, `/ready`), grouped by what they front. All state-changing
routes require [authentication](authentication.md).

| Group | Examples | Purpose |
|---|---|---|
| Framework rows | `/api/dashboard`, `/api/datasets`, `/api/training-runs/{id}`, `/api/models/{id}`, `/api/readiness/{version_id}`, `/api/drift/{version_id}` | Thin façades over the managers — zero new business logic. List endpoints take `limit`/`offset` (default 200, max 1000) and return the unpaged total in `X-Total-Count` |
| Lineage | `/api/lineage/{dataset\|dataset-version\|model-version\|training-run}/{id}` | Lineage graph JSON — see [Lineage](../concepts/lineage.md) |
| Airflow proxy | `/api/airflow/health`, `/api/airflow/dags/{id}`, `/api/training-runs/{id}/tasks` | Live DAG/task state for the pipeline detail page |
| Airflow task control | `/api/training-runs/{id}/tasks/{task_id}/clear`, `.../retry` | Gated write endpoints — fix a stuck task without leaving Gateflow |
| MLflow proxy | `/api/training-runs/{id}/mlflow`, `/api/mlflow/experiments`, `/api/mlflow/registered-models` | Live run/experiment data for the run detail page |
| Settings | `/api/settings` | Effective config + live reachability for the database, MLflow, Airflow |
| Audit trail | `/api/audit` | Who/what triggered a schedule or promotion decision |
| Alerts | `/api/alerts` | What the framework itself detected (training failures, drift, blocked retrains) |
| Start training | `POST /api/training-runs` | Create a run and hand it to Airflow in one gated, audited call — see [Starting a Training Run](start-training.md) |
| Drift | `/api/drift/{id}` (read), `/api/drift/{id}/check` (run) | The check is queued on Airflow, not run in-process — see [Running a Drift Check](drift-check.md) |
| API keys | `/api/api-keys` | Mint (returns the key once), list, revoke. Requires the `admin` scope |
| Rollback | `/api/model-versions/{id}/rollback` | Put a retired version back into production — see [Rolling Back](../governance/rollback.md) |
| Report | `/api/model-versions/{id}/report` | Download a self-contained reproducibility report (`?format=markdown\|html`) |
| Health | `/health`, `/ready` | Liveness (process only) and readiness (pings the database) — mounted at the root, not under `/api`, for container/load-balancer probes |
| Internal | `/api/internal/*` | The Airflow DAG's own callbacks (`resolve_context`, `finish`, `promote`) — the only route into the database from outside the docker network. Gated, whole router, GET included |

!!! tip "Interactive reference"
    The full, always-current list — every route, every schema, "try it
    out" — is served by the running app itself at `/docs` (Swagger UI /
    OpenAPI). This page is the map; `/docs` is the live territory.
