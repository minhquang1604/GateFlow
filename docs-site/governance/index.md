# Governance

Every decision between "a dataset version exists" and "a model is
serving in production" is explicit, explainable, and — for retraining —
chainable into one call.

```
Dataset readiness ──▶ Drift detection ──▶ Training eligibility
                                                  │
                                                  ▼
                                         Human approval (optional)
                                                  │
                                                  ▼
                                              Training
                                                  │
                                                  ▼
                                          Promotion policy
                                                  │
                                                  ▼
                                        Event published / serving reload
```

`RetrainingWorkflow` chains all of this automatically — see
[Automated Retraining Workflow](retraining-workflow.md) — but every
piece below is also usable standalone.

| Decision | Answers | Page |
|---|---|---|
| Dataset Readiness | Is this dataset version fit to train on at all? | [→](readiness.md) |
| Drift Detection | Has the production data's distribution shifted from the training reference? | [→](drift.md) |
| Training Eligibility | Given readiness (and drift, if measured), *should* training happen right now? | [→](eligibility.md) |
| Human Approval | Should a real person confirm before any compute is spent? | [→](approval.md) |
| Promotion Policy | Is this candidate good enough to become the production model? | [→](promotion.md) |
| Rolling Back | Production is broken — put back the version that worked. | [→](rollback.md) |

For a worked, end-to-end example of all of these firing in sequence —
including the human approval gate actually blocking a retrain — see the
[Closed-Loop Demo](../demos/closed-loop-demo.md).
