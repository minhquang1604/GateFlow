# Using the Framework Directly

For framework contributors, or application code that needs governance
primitives the SDK doesn't expose yet. Most application code should
prefer [Using the SDK](using-the-sdk.md).

Working with the managers directly is covered page by page under
**Concepts** and **Governance**:

| Manager / service | Page |
|---|---|
| `DatasetManager` | [Datasets & Versions](../concepts/datasets.md) |
| `TrainingManager`, `TrainingService`, `Orchestrator` | [Training Runs & Orchestration](../concepts/training.md) |
| `ModelManager` | [Model Registry](../concepts/models.md) |
| `ExperimentTracker` | [Experiment Tracking](../concepts/tracking.md) |
| `ServingBridge` | [Serving Bridge](../concepts/serving.md) |
| `LineageManager` | [Lineage](../concepts/lineage.md) |
| `ReadinessEngine` | [Dataset Readiness](../governance/readiness.md) |
| `DriftService` | [Drift Detection](../governance/drift.md) |
| `TrainingEligibilityPolicy` | [Training Eligibility](../governance/eligibility.md) |
| `ModelPromotionPolicy` | [Promotion Policy](../governance/promotion.md) |
| `RetrainingWorkflow` | [Automated Retraining Workflow](../governance/retraining-workflow.md) |
| `ApprovalGate` | [Human Approval](../governance/approval.md) |

## Architecture

See [Architecture](../concepts/architecture.md) for how these compose —
in particular, the dependency direction: the framework depends only on
its own ABCs (`Orchestrator`, `ExperimentTracker`, `DriftDetector`,
`ApprovalGate`, `EventPublisher`); Airflow, MLflow, and Telegram live in
adapter modules imported lazily, so the framework stays importable
without any of them installed.
