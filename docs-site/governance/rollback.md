# Rolling Back

```python
model = project.get_model("fraud-xgboost")
model.rollback_to(3)          # v3 back into production, incumbent archived
```

Or from the console's Model registry page (**Roll back** on any retired
version), or over HTTP:

```bash
curl -X POST localhost:8000/api/model-versions/12/rollback \
  -H "X-Console-Token: $CONSOLE_WRITE_TOKEN" -H "X-Actor: alice"
```

## Why it skips the promotion policy

The [promotion policy](promotion.md) is deliberately **not** consulted.
It answers "is this candidate good enough to replace production",
judged on metrics; a rollback answers "production is broken, put back
the version that worked", and the version being restored already passed
that policy once. Gating it on metrics would block the rollback in
exactly the case it exists for — an incumbent whose offline metrics look
better than the version you need back. The decision is the operator's;
the framework records it loudly instead (audit row, CRITICAL alert)
rather than second-guessing it.

The HTTP route additionally asks the `ServingBridge` to reload, and
reports `serving_reloaded` so a caller can tell "the registry rolled
back and serving followed" from "the registry rolled back and serving
may not have". `MLOpsModel.rollback_to` (the SDK method) changes the
framework's own registry only — the SDK holds no opinion about where
serving lives.
