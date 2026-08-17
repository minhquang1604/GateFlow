# Serving Bridge

A small FastAPI app that atomically reloads whichever `ModelVersion` was
just promoted, and reports which one is currently active.

```python
from mlops_framework.serving import ServingBridge

bridge = ServingBridge(session_factory=get_db_manager().session_factory)
# uvicorn my_app:app --factory   where my_app:app = bridge.app
```

```bash
curl -X POST localhost:8001/internal/model/reload \
  -d '{"model_name": "fraud-model", "model_version": 3, "artifact_uri": "s3://models/fraud-v3.pkl"}'
curl localhost:8001/internal/model/active/fraud-model
```

## How it gets triggered

`RetrainingWorkflow` publishes a `ModelPromotedEvent` through an
`EventPublisher` on every successful promotion — `HttpEventPublisher`
(pointed at `SERVING_BRIDGE_URL`, see
[Configuration](../operations/configuration.md)) is what actually calls
`/internal/model/reload`. `InMemoryEventPublisher` is the drop-in for
tests.

## What a rollback does to it

`POST /api/model-versions/{id}/rollback` additionally asks the
`ServingBridge` to reload and reports `serving_reloaded`, so a caller
can tell "the registry rolled back and serving followed" from "the
registry rolled back and serving may not have". See
[Rolling Back](../governance/rollback.md).
