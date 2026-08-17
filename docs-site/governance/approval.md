# Human Approval

An automated retrain that a policy allows is not always one you want run
unattended. `RetrainingWorkflow` takes an optional gate, asked *after*
eligibility and *before* training — no point asking a human about a
retrain the policies already ruled out, and asking before any compute is
spent is the value of the gate.

```python
from mlops_framework.approval.telegram import TelegramApprovalGate

workflow = RetrainingWorkflow(
    session,
    training_service=service,
    approval_gate=TelegramApprovalGate.from_settings(get_settings()),
)
outcome = workflow.run(dataset_version=v, model=m)
# denied -> outcome.blocked_reason == "approval_denied", no TrainingRun created
```

## Deny by default

Every gate **denies by default**: a timeout, an unreachable channel, a
malformed reply all return `approved=False`. A gate that could not reach
anyone has not been told yes, and failing open would make it worse than
having none — it would fail open exactly when something is already
wrong. A denial is recorded the way a policy block is (a `RUN_BLOCKED`
event and a `blocked_reason`), because downstream it is the same fact.

## The `ApprovalGate` family

`ApprovalGate` is an ABC like `DriftDetector` and `EventPublisher`;
Telegram is a reference adapter.

| Gate | Behaviour | Use |
|---|---|---|
| `TelegramApprovalGate` | Sends an Approve/Deny prompt, blocks until answered or timeout | Production |
| `AutoApproveGate` | Always approves | Tests, or a caller that wants the audit record of a gate without a human in the loop |
| `DenyAllGate` | Always denies | Tests, or demonstrating the safety invariant |
| `RecordedDecisionGate` | Replays a decision obtained earlier through another channel | A caller that must ask *before* the workflow's own gate point (see below), without asking twice or losing the audit row |

## Asking earlier than the workflow does

`RetrainingWorkflow` asks after eligibility, right before training —
the right default. But a caller that builds the training data itself
only once a retrain is authorised (constructing dataset V2 = V1 + new
data is real work that shouldn't happen speculatively) needs the answer
*before* that point. `RecordedDecisionGate` is built for exactly this:
ask the human once, hand the workflow the answer, and the workflow still
writes its own `RETRAIN_APPROVED`/`RETRAIN_DENIED` audit row. The
[Closed-Loop Demo](../demos/closed-loop-demo.md#how-approval-works)
uses this pattern.

## Demonstrating the safety invariant

Run the closed-loop demo with `--decision reject` at least once — it's
the shorter, more convincing half of the story: drift is detected, the
alert goes out, the admin says no, and *nothing else happens*.
