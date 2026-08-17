# Project Structure

```
Framework/
├── src/mlops_framework/     # framework source — see Architecture
├── case_studies/            # fraud_detection/, customer_churn/ — SDK consumers
├── demo/                    # closed-loop demo — runner, steps/, its own README
│   ├── run_closed_loop_demo.py
│   ├── config.py             # every seed/threshold, in one place
│   ├── steps/                # one module per lifecycle phase
│   └── data/                 # generated datasets (gitignored, bind-mounted)
├── scripts/                 # other end-to-end demo entry points
├── tests/
│   ├── unit/                 # unit tests
│   ├── integration/          # integration + governance e2e tests
│   ├── api/                  # FastAPI TestClient tests
│   ├── demo/                 # closed-loop lifecycle + safety-invariant tests
│   └── _pipelines/           # fixture pipelines for orchestrator tests
├── infrastructure/
│   ├── airflow/               # Dockerfile, entrypoint.sh, dags/
│   ├── app/                   # Dockerfile for app/serving/demo
│   ├── mlflow/, minio/, postgres/
│   └── terraform/             # AWS deployment
├── alembic/                  # versions/, env.py
├── docs/specs/               # one spec per feature, for contributors
├── docs-site/                # this documentation site's source (mkdocs)
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

See [Architecture](../concepts/architecture.md) for the module layout
under `src/mlops_framework/`.
