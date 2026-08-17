# Known Limitations

1. **`LocalDockerOrchestrator` is a subprocess shim, not Docker.** The
   name carries forward compatibility with a future real Docker-based
   implementation; the `Orchestrator` interface is identical, so a
   `DockerOrchestrator` would be a drop-in.
2. **`RetrainingWorkflow` + `AirflowOrchestrator` needs the DAG's
   cooperation, not just `training_entrypoint`.** The framework side
   is handled — metrics reported out-of-band survive
   `wait_for_completion()`'s own status merge, and `RetrainingWorkflow`
   itself never races anyone to close out the run. But
   `mlops_training_pipeline.py`'s `register_and_promote` and
   `report_status` tasks own registration/promotion by default (the
   right behavior for demo scripts that call the DAG directly, not
   through `RetrainingWorkflow`) — they only step back when the run's
   metadata carries `owned_by_workflow` (set automatically by
   `RetrainingWorkflow.run()`). A custom DAG that doesn't check that
   flag would double-register a `ModelVersion` and evaluate it against
   two different promotion policies. See that DAG's module docstring.
3. **MLflow registry writes are not transactional with the framework
   database.** `RetrainingWorkflow` registers and stages a version in
   MLflow (`tracking/mlflow_registry.py`) inside the same block that
   later commits the framework's own rows. If that transaction rolls
   back — a crash between promotion and `commit()` — the framework
   correctly keeps the previous model in `PRODUCTION`, but MLflow
   retains the registered version, so the two registries disagree
   until the next successful run. The framework side is the source of
   truth and stays safe; the MLflow side needs manual tidying. Observed
   in practice, not theoretical.
4. **Airflow "cancel" deletes the DAG run.** Airflow 2.x has no clean
   REST endpoint to cancel a running DAG run; deletion is the
   documented workaround (`AirflowOrchestrator.cancel_execution`).
5. **`TrainingService.wait_for_completion` polls** rather than using a
   callback/event bus.
6. **MLflow is optional.** The framework never requires it to import
   or run — `MLflowTracker` fails with a clear framework-level error
   if `mlflow` isn't installed, and `InMemoryTracker` is a drop-in for
   tests.
7. **`CONSOLE_WRITE_TOKEN` is still accepted, and is not
   authentication.** Scoped API keys are now the real credential (see
   [Authentication](../api/authentication.md)); the shared secret is
   kept because it is what every existing deployment — the Airflow DAG
   included — is configured with, and removing it in the same change
   that introduced keys would break all of them at once. A request
   authenticated that way still records the unverified `X-Actor`
   header as its actor. Migrate the DAG and any scripts to keys, then
   unset it.
8. **There is no browser login.** The console prompts for a credential
   and keeps it in `sessionStorage` for the tab. Read endpoints are
   ungated, so the console renders for anyone who can reach it —
   gating GETs needs session management the app does not have.

See also the [Closed-Loop Demo's failure handling table](../demos/closed-loop-demo.md#failure-handling)
for what happens when a specific stage of a retrain fails.
