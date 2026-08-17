# Installation (local, SQLite)

For exploring the framework without Docker or Postgres:

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"        # runtime + dev deps (pytest, ruff, httpx)
pip install mlflow              # optional — only needed for MLflowTracker

echo 'DATABASE_URL=sqlite:///./mlops.db' >> .env
alembic upgrade head
```

Start Gateflow + the API:

```bash
uvicorn mlops_framework.api.app:create_app --factory --reload
```

- `http://localhost:8000/` — Gateflow
- `http://localhost:8000/docs` — interactive OpenAPI docs (Swagger UI)
- `http://localhost:8000/api/dashboard` — JSON KPIs

## Using Postgres instead

```bash
cp .env.example .env
docker compose up -d postgres
alembic upgrade head
```

`.env` is loaded by both `pydantic-settings` and Alembic — no
credentials live in `alembic.ini` or any Python file.

## Next

- [Configuration](../operations/configuration.md) — every environment
  variable the framework reads, and what it's for.
- [Database Migrations](../operations/migrations.md) — the Alembic
  revision history.
- [Using the SDK](../sdk/using-the-sdk.md) — write your first pipeline
  against the framework.
