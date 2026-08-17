# Lineage

Answers, from **any** node in the chain, "where did this come from, and
what did it lead to":

```
DatasetVersion → TrainingRun → ModelVersion → ServingInstance
```

plus `DatasetVersion → DatasetVersion` (`derived_from`) whenever a
version was built by extending an earlier one — the retraining case,
where V2 = V1 plus the production data that drifted.

```python
from mlops_framework.lineage import LineageManager

graph = LineageManager(session).graph_for_model_version(mv.id)
for node in graph.nodes:
    print(node.type, node.label, node.attributes)   # TrainingRun nodes carry pipeline_id + mlflow_run_id
for edge in graph.edges:
    print(edge.source, "->", edge.target, f"({edge.type})")
```

## One node per version, not two

A dataset's or model's name lives on its version node's own label
(`"credit-card-fraud v1"`, `"fraud-xgboost v2"`) — there is no separate,
un-versioned `Dataset`/`Model` identity node joined by a `has_version`
edge. Four node types total: `DatasetVersion`, `TrainingRun`,
`ModelVersion`, `ServingInstance`.

## Every version, in parallel

Every entry point — `graph_for_dataset`, `graph_for_dataset_version`,
`graph_for_model_version`, `graph_for_training_run` — returns the
**same** graph: every version of a dataset, each with its full
downstream (every training run, every model version, every serving
instance), not just the ancestors/descendants of whichever node you
started from. A dataset with an archived V1 and a production V2 shows
both branches side by side no matter which node you click into; only
`root_id` (which node gets the highlight ring) changes.

In the Gateflow console's lineage graph, every `DatasetVersion` lines up
in one column, every `TrainingRun` in the next, and so on — same type,
same column — so two branches read as two aligned rows rather than a
staggered mess.

## Where it's exposed

| Interface | |
|---|---|
| Python | `LineageManager(session).graph_for_*(id)` |
| HTTP | `GET /api/lineage/{dataset\|dataset-version\|model-version\|training-run}/{id}` — see [REST API Reference](../api/reference.md) |
| Console | `/lineage` — see [Gateflow Console](../console.md) |
| Reproducibility report | `project.report(model_version_id)` walks this same graph — see [Using the SDK](../sdk/using-the-sdk.md) |
