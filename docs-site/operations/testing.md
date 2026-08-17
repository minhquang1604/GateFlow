# Testing

```bash
pytest                         # full suite
pytest tests/unit              # unit tests only
pytest tests/integration       # integration tests only
pytest -k drift                # by name
pytest --cov=mlops_framework   # coverage
```

Every test carries a 300s ceiling (`pytest-timeout`, configured in
`pyproject.toml`). Several drive real subprocesses and a real local
MLflow store, so a hang there used to stall the whole run with nothing
naming the test responsible.

## The suite is hermetic

No live MLflow, Airflow, or Postgres required for the default run:

- `LocalDockerOrchestrator` unit tests spawn real Python subprocesses
  against fixture pipelines in `tests/_pipelines/`.
- `AirflowOrchestrator` unit tests use a fake `httpx.Client`.
- Integration tests use in-memory SQLite (`StaticPool`).
- `tests/api/` boots the real FastAPI app via `TestClient` against an
  in-memory database.
- `tests/demo/` (the [Closed-Loop Demo](../demos/closed-loop-demo.md))
  substitutes `LocalDockerOrchestrator` + `InMemoryTracker` for
  Airflow/MLflow, so every governance decision is verified without the
  full stack.

A handful of integration tests need a live stack
(`docker compose up -d`) and are opt-in — see
`tests/integration/test_airflow_live.py`.

## CI

CI runs `ruff check .` over the whole repository, not just `src/`, and
`pytest tests/ case_studies/` — `pytest tests/` alone would skip the
two case studies that exist to prove the SDK boundary holds. Deliberate
lint exceptions live in `pyproject.toml`'s
`[tool.ruff.lint.per-file-ignores]`, each with the reason written next
to it.
