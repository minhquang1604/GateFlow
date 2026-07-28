"""Fraud Detection case study.

This case study uses the MLOps Framework **only through the public SDK**.
No direct imports of managers, services, or database models — that is
verified by ``tests/test_use_case.py``.

Pipelines (entry points called by the orchestrator):

* :func:`pipelines.train_baseline` — baseline XGBoost-style fraud model.
* :func:`pipelines.train_advanced` — second iteration with engineered
  features (demonstrates a different pipeline on the same SDK).
* :func:`pipelines.fail` — pipeline that always fails (used in tests
  to verify the SDK surfaces a :class:`TrainingError`).

App-level entry points:

* :func:`app.run_full_lifecycle` — full SDK happy path: create dataset,
  add a version, train, inspect, and lineage.

The case study is intentionally tiny: no real data, no external
dependencies, no ML library. The point is to demonstrate that the
framework is reusable.
"""
