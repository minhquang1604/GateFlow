# Database Migrations

```bash
alembic upgrade head      # apply all migrations
alembic current            # show current revision
alembic history             # show full migration history
alembic revision -m "..."  # create a new empty migration
alembic downgrade -1        # roll back one revision
```

| Revision | Adds |
|---|---|
| `001_initial` | `datasets`, `dataset_versions`, `training_runs` |
| `002_training_run_lifecycle` | `pipeline_id`, `mlflow_run_id`, `error_message` on `training_runs` |
| `003_models` | `models`, `model_versions`, `model_state_enum` |
| `004_week3_governance` | `readiness_evaluations`, `drift_evaluations`, `model_promotion_events`, `serving_instances` |
| `005_schedules` | `schedules` — cron-cadence retraining jobs |
| `006_one_production_per_model` | DB constraint: at most one `PRODUCTION` `ModelVersion` per model |
| `007_audit_log` | `audit_logs` — append-only "who did what" |
| `008_governance_events` | `governance_events` — conditions the framework itself detects |
| `009_framework_settings` | `framework_settings` — persisted overrides for the governance policy dataclasses |
| `010_api_keys` | `api_keys` — see [Authentication](../api/authentication.md) |
| `011_dataset_version_lineage` | `dataset_versions.parent_version_id` (self-referencing FK) — see [Lineage](../concepts/lineage.md) |

See [Database Schema](../reference/database-schema.md) for how the
resulting tables relate to each other.
