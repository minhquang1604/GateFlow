# Database Schema

```text
┌─────────────┐         ┌───────────────────┐         ┌───────────────┐
│  datasets   │         │ dataset_versions  │         │ training_runs │
├─────────────┤         ├───────────────────┤         ├───────────────┤
│ id (PK)     │──1:N──▶│ id (PK)           │──1:N──▶│ id (PK)       │
│ name (U)    │         │ dataset_id (FK)   │         │ dataset_ver   │
│ description │         │ version_number    │         │   _id (FK)    │
│ created_at  │         │ storage_uri       │         │ pipeline_id   │
│ updated_at  │         │ checksum (SHA256) │         │ status        │
└─────────────┘         │ schema_hash       │         │ trigger_type  │
                        │ row_count         │         │ started_at    │
                        │ metadata_json     │         │ completed_at  │
                        │ is_immutable      │         │ mlflow_run_id │
                        │ parent_version_id │         │               │
                        │   (FK, self)      │         │               │
                        │ created_at        │         │ error_message │
                        │ updated_at        │         │ metadata_json │
                        └────┬──────────────┘         │ created_at    │
                             │                        │ updated_at    │
              ┌──────────────┼─────────────┐          └───────┬───────┘
              │              │             │                  │
              ▼ 1:N          ▼ 1:N         ▼ 1:N              │ N:1
   ┌──────────────────┐ ┌──────────────┐ ┌────────────┐      │
   │ readiness_evals  │ │ drift_evals  │ │ model_     │      │
   ├──────────────────┤ ├──────────────┤ │ versions   │◀─────┘ (SET NULL)
   │ id (PK)          │ │ id (PK)      │ ├────────────┤
   │ dataset_version  │ │ reference_   │ │ id (PK)    │
   │   _id (FK)       │ │   dataset_   │ │ model_id   │
   │ status (enum)    │ │   version_id │ │   (FK)     │──▶┌────────────┐
   │ checks_json      │ │ current_     │ │ dataset_   │   │  models    │
   │ reasons_json     │ │   dataset_   │ │   version_ │   ├────────────┤
   │ policy_json      │ │   version_id │ │   id (FK)  │   │ id (PK)    │
   │ snapshot_json    │ │ method       │ │ training_  │   │ name (U)   │
   │ observed_        │ │ outcome      │ │   run_id   │   │ task       │
   │   row_count      │ │ score        │ │ version_   │   │ description│
   │ created_at       │ │ threshold    │ │   number   │   │ created_at │
   │ updated_at       │ │ details_json │ │ state      │   │ updated_at │
   └──────────────────┘ │ created_at   │ │ mlflow_run │   └─────┬──────┘
                        │ updated_at   │ │ artifact_  │         │ 1:N
                        └──────────────┘ │   uri      │         ▼
                                       │ metrics_   │ ┌─────────────────┐
                                       │   json     │ │ serving_        │
                                       │ notes      │ │ instances       │
                                       │ created_at │ ├─────────────────┤
                                       │ updated_at │ │ id (PK)         │
                                       └─────┬──────┘ │ serving_inst_id │
                                             │        │ model_id (FK)   │
                                             │        │ model_version   │
                                             │        │   _id (FK)      │
                                             │        │ is_active       │
                                             │        │ reload_source   │
                                             │        │ created_at      │
                                             │        │ updated_at      │
                                             ▼        └─────────────────┘
                                       ┌──────────────────┐
                                       │ model_promotion_ │
                                       │ events           │
                                       ├──────────────────┤
                                       │ id (PK)          │
                                       │ event_type       │
                                       │ model_id (FK)    │
                                       │ model_version_   │
                                       │   id (FK)        │
                                       │ model_name       │
                                       │ model_version_   │
                                       │   number         │
                                       │ artifact_uri     │
                                       │ metrics_json     │
                                       │ status (enum)    │
                                       │ published_at     │
                                       │ error_message    │
                                       │ created_at       │
                                       │ updated_at       │
                                       └──────────────────┘
```

`dataset_versions.parent_version_id` is what
[Lineage](../concepts/lineage.md) walks to draw the `derived_from`
edge between a dataset version and the one it was built by extending.

Not pictured: `schedules`, `audit_logs`, `governance_events`,
`framework_settings`, `api_keys` — see
[Database Migrations](../operations/migrations.md) for what each adds.
