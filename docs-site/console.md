# Gateflow Console

A server-rendered console (HTML + vanilla JS, no build step) served on
the same FastAPI app as the API, at `/`.

| Page | Route | Shows |
|---|---|---|
| Dashboard | `/dashboard` | Dataset/run/model counts, success rate |
| Datasets | `/datasets/{id}` | Versions, schema, readiness panel, **drift panel** with a **Run check** button, **Train now** on any version |
| Training runs | `/runs/{id}` | Params, metrics, error, MLflow panel, Airflow task grid (per-task Clear/Retry, gated — see [Configuration](operations/configuration.md)'s `CONSOLE_WRITE_TOKEN`) |
| Models | `/models/{id}` | Versions, metrics, production state, per-version **reproducibility report** download, and **Roll back** on any retired version |
| Pipelines | `/pipelines/{dag_id}` | Airflow DAG Graph View + task-instance history grid |
| Lineage | `/lineage` | Every dataset version, in parallel, click-through — see [Lineage](concepts/lineage.md) |
| Settings | `/settings` | Effective MLflow/Airflow/database config, secrets masked, live reachability ping |
| Activity | `/activity` | Two tabs: **Audit trail** (who/what triggered a schedule or promotion decision) and **Alerts** (what the framework itself detected — training failures, drift, blocked retrains) |

## Authentication in the console

There is no browser login session — the console prompts for a
credential and keeps it in `sessionStorage` for the tab. Read endpoints
are ungated, so the console renders for anyone who can reach it; see
[Authentication](api/authentication.md) for what write actions require,
and [Known Limitations](operations/known-limitations.md) for the full
caveat.

## Under the hood

Every page in Gateflow is a thin client over the same
[REST API](api/reference.md) documented elsewhere on this site — there
is no server-side logic in the console that the API doesn't also
expose.
