# Configuration

Environment variables (`.env` for host runs / Alembic, `.env.docker`
for `docker compose`) — see `.env.example`:

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy database URL | `postgresql+psycopg://postgres:postgres@localhost:5432/mlops_framework` |
| `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` / `DATABASE_POOL_TIMEOUT` | Connection pool tuning | `5` / `10` / `30` |
| `DATABASE_ECHO` | Echo SQL to stdout | `false` |
| `MLFLOW_TRACKING_URI` | MLflow tracking server URL used by `MLflowTracker` | unset (falls back to `http://localhost:5000` in scripts) |
| `MLFLOW_EXPERIMENT_NAME` | Default MLflow experiment | `mlops-framework` |
| `MLFLOW_S3_ENDPOINT_URL` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | MinIO/S3 credentials the MLflow **client** needs directly — it talks to the artifact store, bypassing the mlflow server. Missing these fails `log_artifact`/`download_artifacts` with a silent `AccessDenied`, not a crash | unset |
| `AIRFLOW_BASE_URL` / `AIRFLOW_USERNAME` / `AIRFLOW_PASSWORD` | `AirflowOrchestrator` REST credentials | unset / `airflow` / `airflow` |
| `SERVING_BRIDGE_URL` | Used by `HttpEventPublisher` | unset |
| `CONSOLE_WRITE_TOKEN` | Shared secret required in the `X-Console-Token` header by **every** state-changing endpoint: all of `/api/internal/*` (the Airflow DAG's callbacks) and the write half of `/api/schedules`, plus Airflow task Clear/Retry. The gate fails closed: unset, those endpoints answer 503, so the DAG cannot report a run back and Scheduling is read-only. Reads are never gated | unset |
| `APP_NAME` / `APP_VERSION` | Application metadata | `mlops-framework` / `0.1.0` |
| `DEBUG` | Debug mode | `false` |

`alembic.ini` holds no credentials — `alembic/env.py` loads
`DATABASE_URL` from `.env` via `python-dotenv` and fails fast if it's
missing.

Inside `docker compose`, `app`/`serving`/`demo`/`airflow-webserver`/
`airflow-scheduler` already have the MinIO credentials and in-network
service URLs (`http://mlflow:5000`, `http://airflow-webserver:8080`, …)
set in `docker-compose.yml` — you only need to export them yourself
when running a script on the host.

See also the [Closed-Loop Demo's environment variables](../demos/closed-loop-demo.md#environment-variables)
for the additional ones that demo reads.
