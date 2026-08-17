# Closed-Loop Demo

One reproducible story, from an initial dataset to an automatically
retrained and promoted model — driven end to end by the framework, with
a real human decision in the middle.

```
Dataset V1 ──▶ Training ──▶ Model V1 ──▶ PRODUCTION
                                            │
                                            ▼
                                   Production inference
                                            │
                        ┌───────────────────┴───────────────────┐
                        ▼                                       ▼
              baseline window                        + controlled shift
                        │                                       │
                        ▼                                       ▼
                 Drift detection ◀───── same detector ────▶ Drift detection
                        │                                       │
                     NO DRIFT                              DRIFT DETECTED
                        │                                       │
                        ▼                                       ▼
               continue monitoring                      Drift event persisted
                                                                │
                                                                ▼
                                                      Telegram alert to admin
                                                                │
                                            ┌───────────────────┴──────────┐
                                          Reject                        Approve
                                            │                              │
                                            ▼                              ▼
                                    Keep Model V1              Dataset V2 = V1 + drifted data
                                    Monitoring continues                   │
                                                                           ▼
                                                                    Train Model V2
                                                                    (real Airflow DAG)
                                                                           │
                                                                           ▼
                                                                      Validation
                                                              ┌────────────┴────────────┐
                                                            Fail                       Pass
                                                              │                          │
                                                              ▼                          ▼
                                                       Reject V2                  Register V2
                                                       Keep V1                    Archive V1
                                                                                  Promote V2
                                                                                        │
                                                                                        ▼
                                                                                Resume monitoring
```

!!! abstract "The invariant the whole design exists to provide"
    A failed, rejected, or unapproved retrain never replaces the
    currently valid production model.

## Quick start

The demo needs the full stack: Postgres, MinIO, MLflow, Airflow, the
management API, and the serving bridge.

```bash
cp .env.example .env.docker

docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker run --rm app alembic upgrade head

docker compose --env-file .env.docker --profile demo run --rm demo
```

Then open:

| URL | What you'll see |
|---|---|
| <http://localhost:8000> | **Gateflow** — both models, the drift alert, the approval trail, the lineage graph |
| <http://localhost:5000> | MLflow — both training runs, params, metrics, artifacts |
| <http://localhost:8080> | Airflow — both DAG runs (`airflow`/`airflow`) |
| <http://localhost:9001> | MinIO — the logged model artifacts (`minioadmin`/`minioadmin`) |

### Running it against a stack that is already up

```bash
PYTHONPATH=src:. python -m demo.run_closed_loop_demo --mode interactive
```

### Modes

| Flag | Behaviour |
|---|---|
| `--mode auto` | Runs straight through. For a presentation that needs the whole loop in one take. |
| `--mode interactive` | Pauses before each of the nine phases so the presenter can explain what is about to happen. |

Both take exactly the same path through the framework — interactive
mode adds prompts, not different behaviour.

### Choosing how the admin answers

| Flag | Channel | Use |
|---|---|---|
| `--decision telegram` | Real Telegram, blocks on a real button press | The live demo |
| `--decision approve` | `AutoApproveGate` | Presentation without a phone; CI |
| `--decision reject` | `DenyAllGate` | **Demonstrating the safety invariant** |

Defaults to `telegram` when credentials are configured, otherwise
`approve`.

!!! tip "Run `--decision reject` at least once"
    It's the shorter and more convincing half of the story: drift is
    detected, the alert goes out, the admin says no, and *nothing else
    happens* — no Dataset V2, no training run, no model version. V1 is
    still serving.

## Architecture

Nothing in `demo/` implements governance. Every decision is made by a
framework component, and the demo's job is to sequence them and show
their work.

```
demo/run_closed_loop_demo.py     orchestration, modes, phase sequencing
├── config.py                    every seed, threshold and parameter
├── context.py                   threaded ids + printed lifecycle state
├── reporting.py                 terminal presentation (no decisions)
└── steps/
    ├── initial_training.py  ──▶ scripts/_initial_training.run_initial_training
    ├── _monitoring.py       ──▶ DriftService + ScipyDriftDetector
    ├── simulate_production.py   baseline window        (negative control)
    ├── inject_drift.py          controlled shift       (recorded parameters)
    ├── detect_drift.py      ──▶ DriftEvaluation + GovernanceEvent
    ├── request_approval.py  ──▶ ApprovalGate (Telegram / Auto / Deny)
    ├── build_dataset_v2.py  ──▶ DatasetManager.create_version(parent_version_id=…)
    ├── retrain.py           ──▶ RetrainingWorkflow.run()   ← the whole loop
    ├── validate_model.py        evidence for the promotion decision
    └── finalize.py              final state, re-queried from the database
```

The retrain is **one** framework call. `RetrainingWorkflow.run()` chains
readiness → drift → eligibility → approval → training → candidate
registration → promotion policy → archive-previous → promote → publish
event, and returns a step trace explaining every decision — see
[Automated Retraining Workflow](../governance/retraining-workflow.md).

### Two datasets, not one

| Dataset | Holds | Why separate |
|---|---|---|
| `credit-card-fraud` | V1, then V2 | The training population |
| `credit-card-fraud-production` | Each observed window | Traffic is an *observation of the world*, not a version of the training set |

Collapsing these is what makes "which data drifted?" unanswerable
later. Drift always compares a production window against the
**training reference** (V1) — not against the previous window, which
would measure how fast traffic changes rather than whether the
deployed model's assumptions still hold.

## What each phase does

| # | Phase | Produces |
|---|---|---|
| 1 | Initial training | Dataset V1, Model V1 in `PRODUCTION`, MLflow run, Airflow DAG run |
| 2 | Baseline monitoring | A `DriftEvaluation` with outcome `NO_DRIFT` |
| 3 | Noise injection | A drifted window + its full generation parameters |
| 4 | Drift detection | `DriftEvaluation` (`DRIFT_DETECTED`) + a `CRITICAL` `GovernanceEvent` |
| 5 | Alert + approval | Telegram message + `AuditLog` row naming the responder |
| 6 | Dataset V2 | A `DatasetVersion` with `parent_version_id` → V1 |
| 7 | Retrain | Training run, `ModelVersion` V2, promotion decision |
| 8 | Validation | The three-way metric comparison behind that decision |
| 9 | Final state | Lineage graph + state re-queried from the database |

## Configuration

**Every** parameter lives in `demo/config.py`. Nothing else in `demo/`
hard-codes a threshold or reads an environment variable.

Key values:

| Parameter | Default | Meaning |
|---|---|---|
| `n_rows` | 8000 | Rows in Dataset V1 |
| `fraud_ratio` | 0.02 | Positive-class rate |
| `seed` | 42 | V1 generation seed |
| `window_rows` | 1000 | Rows per production window |
| `normal_window_seed` | 1001 | Baseline window seed |
| `drifted_window_seed` | 2002 | Drifted window seed |
| `drifted_drift_shift` | 1.0 | Shift magnitude |
| `drift_threshold` | 0.05 | Family-wise significance level |
| `drift_correction` | `bonferroni` | Multiple-comparisons correction |
| `required_rows` | 1000 | Readiness floor |
| `promotion_min_metrics` | `f1≥0.70, precision≥0.70` | Acceptance criteria |
| `must_beat_production` | `False` | See [validation](#how-model-v2-is-validated) |

The config is a **frozen** dataclass, so no phase can quietly retune
the experiment mid-run, and `to_dict()` is printed at the start of
every run as the reproducibility record.

Only *placement* is environment-driven (`DEMO_DATA_DIR`,
`DEMO_AIRFLOW_DATA_DIR`, `AIRFLOW_DAG_ID`). Seeds and thresholds
deliberately are not: an experiment whose parameters depend on the
shell it ran in is not reproducible.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Framework metadata store |
| `MLFLOW_TRACKING_URI` | yes | Tracking server this process talks to |
| `AIRFLOW_INTERNAL_MLFLOW_URI` | in Docker | Tracking URI reachable **from the Airflow container** (`http://mlflow:5000`) |
| `AIRFLOW_BASE_URL` | yes | Airflow REST API |
| `SERVING_BRIDGE_URL` | yes | Serving bridge |
| `CONSOLE_WRITE_TOKEN` | yes | Gates `/api/internal/*`, which the DAG calls back into |
| `DEMO_DATA_DIR` | no | Where generated CSVs are written (`/opt/demo_data` in Docker) |
| `TELEGRAM_BOT_TOKEN` | for `--decision telegram` | Bot credential |
| `TELEGRAM_ADMIN_CHAT_ID` | for `--decision telegram` | Chat to notify |

Credentials are read from the environment only. None are ever written
to a dataset, an audit row, or the alert message.

### The shared data mount

Generated datasets are written to `demo/data/`, bind-mounted into the
Airflow containers at `/opt/demo_data`.

This is what makes retraining through the real DAG possible.
`case_studies/` is baked into the Airflow image at build time, so a
path under it exists inside Airflow only for files committed *before*
the build — and Dataset V2 is constructed *during* the run by
definition. The bind mount removes both that limitation and the
"commit the CSV, then rebuild the images" setup step an earlier
revision of this demo required.

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`,
   follow the prompts, and copy the token.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `result[0].message.chat.id`.
3. Put both in `.env.docker`:

    ```bash
    TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
    TELEGRAM_ADMIN_CHAT_ID=987654321
    ```

The bot sends one message with **Approve** / **Deny** buttons and
blocks until pressed or `approval_timeout` (default 1 hour) elapses.

If Telegram is unreachable, unconfigured, or times out, the decision is
**denied** — a `GovernanceEvent` records the delivery failure, the
drift event is left untouched, and no retrain happens. An admin who
was never reached has not said yes.

## How approval works

Drift detection does **not** authorise a retrain. The framework has the
evidence and could start training immediately; it asks instead.

The human is asked **once**, before Dataset V2 is built — because
building V2 is work that should not happen speculatively, and
`RetrainingWorkflow` consults its gate later than that. The answer is
then handed to the workflow as a `RecordedDecisionGate` (see
[Human Approval](../governance/approval.md)), so:

- the human is not asked twice,
- the workflow still writes its own `RETRAIN_APPROVED` /
  `RETRAIN_DENIED` audit row,
- and a denial recorded earlier still stops the retrain inside the
  workflow — the gate is not a formality that has been
  short-circuited.

Two audit rows result, deliberately: `RETRAIN_REQUEST_APPROVED` (the
moment the question was answered, by the admin) and `RETRAIN_APPROVED`
(the workflow acting on it). Both name the responder.

## How Dataset V2 is built

```
dataset_v1.csv  (8,000 rows, the reference population)
        +
production_window_drifted.csv  (1,000 rows, observed and shifted)
        ↓  concat, in order, header once
dataset_v2.csv  (9,000 rows)
```

V2 **extends** V1; it does not replace it.

- Training only on the drifted window would produce a model that
  handles today's traffic and has forgotten the population it was
  already serving correctly.
- Regenerating V1 at the new distribution would be worse: it rewrites
  history so the "before" population never existed, and the lineage
  then claims the model trained on data nobody observed.

Registration records `parent_version_id → V1` plus a `derivation` block
naming both source versions, their row counts, their content hashes,
and the drift event that justified the merge. The step refuses to
register V2 if its row count is not exactly V1 + window.

`LineageManager` walks that parent edge (see [Lineage](../concepts/lineage.md)),
so the graph — and the `/api/lineage` endpoint, and the Gateflow
console — can answer:

```
Model V2
  ← trained from Dataset V2
  ← Dataset V2 = Dataset V1 + production window
  ← because drift event N was detected
  ← on that window, measured against Dataset V1
  ← retraining approved by an administrator
```

## How Model V2 is validated

Retraining succeeding is **not** grounds for promotion.
`ModelPromotionPolicy` evaluates the candidate before it goes anywhere
(see [Promotion Policy](../governance/promotion.md)), and Phase 8
prints the evidence behind that decision:

| Metric | V1 (stored) | V1 (live) | V2 | Required |
|---|---|---|---|---|
| f1 | 0.9032 | **0.5067** | 0.8732 | 0.70 |
| precision | 0.9333 | **0.3455** | 0.8857 | 0.70 |
| recall | 0.8750 | 0.9500 | 0.8611 | — |
| roc_auc | 0.9984 | 0.9955 | 0.9988 | — |

*(Measured values from an actual run, not illustrations.)*

Read the middle column carefully: V1's **recall went up**
(0.875 → 0.950) while its **precision collapsed** (0.933 → 0.346).
That is the signature of exactly the shift that was injected —
legitimate traffic moved toward the fraud cluster, so the unchanged
decision boundary now catches nearly every real fraud *and* floods the
queue with false positives. A summary that reported only ROC-AUC would
have shown 0.9955 and concluded nothing was wrong.

Three columns, not two:

- **V1 stored** — measured at training time, on a population that no
  longer exists.
- **V1 live** — V1's actual artifact, re-scored *now* on the drifted
  window. This is the honest statement of the problem.
- **V2** — the candidate.

The middle column is why `must_beat_production=False`. Once the
population has shifted, V1's stored metric is not a fair bar in either
direction, so the policy gates on an **absolute quality floor**
instead. Comparing V2 against a number measured on vanished data would
be the easiest way to make a retrain look justified when it is not.

If the live re-score cannot be obtained, the column is left empty
rather than filled in. A gap in the evidence is a gap; an invented
number is a false claim, and this is the measurement the whole
argument rests on.

On pass: V2 registered → **V1 archived** → V2 promoted (in that order,
so nothing ever observes two `PRODUCTION` versions). On fail: V2
marked `REJECTED`, V1 untouched.

## Drift detection and why it is corrected

Two decisions here are load-bearing and both are departures from the
framework defaults (see [Drift Detection](../governance/drift.md)).

**`time` is not monitored.** It is seconds since the first
transaction — a counter, not a covariate. Any two windows of different
length have different `time` distributions *by construction*, so a KS
test on it reports drift every run and reports nothing about the data.
It stays in `feature_columns()` because the model may learn from it;
"what the model reads" and "what we monitor for shift" are different
questions. See `monitored_feature_columns()`.

**Bonferroni correction is on.** 29 features are compared per window.
Testing each at α=0.05 and declaring drift if *any* is significant
gives a family-wise false-positive rate of

```
1 - 0.95^29 ≈ 0.77
```

— the baseline window would be flagged in roughly three runs out of
four, and the negative control that makes the real detection
meaningful would be worthless. Dividing the threshold by the number of
features tested holds the family-wise error rate at 0.05.

Measured effect on this experiment:

| Window | Uncorrected | Bonferroni |
|---|---|---|
| baseline (no shift) | **DRIFT** — `v3`, `v26` (false positives) | NO DRIFT |
| drifted | DRIFT — 9 features incl. `v7`, `v19` (false positives) | DRIFT — exactly the 7 injected: `amount`, `v1`–`v6` |

The framework default remains `correction="none"` for backwards
compatibility; the demo opts in via `DemoConfig.drift_correction`.

## Why the workflow's own drift check reports NO drift

A detail worth understanding before presenting this, because it looks
like a contradiction and is not.

`RetrainingWorkflow` runs its own drift step, comparing the
**candidate dataset** against its predecessor — V2 against V1. That
step reports **no drift**, in the same run where the alert reported
drift decisively. Both are correct, because they ask different
questions:

| Comparison | Sample | Max KS | Verdict at α/29 |
|---|---|---|---|
| production window vs V1 | 1,000 shifted rows vs 8,000 | 0.2490 | **DRIFT** |
| V2 vs V1 | 9,000 rows, of which 8,000 *are* V1 | 0.0277 | no drift |

V2 contains V1. The 1,000 shifted rows are diluted to about a ninth of
the sample, which pushes the statistic below the corrected critical
value (~0.0289). The incoming traffic shifted; the training set barely
moved, because it absorbed the shift into a much larger reference
population — which is precisely what "extend V1 rather than replace
it" was supposed to achieve.

This is why `require_drift_to_retrain` is set to **False** here (see
[Training Eligibility](../governance/eligibility.md)). Leaving it
`True` would gate the retrain on that diluted test. It appeared to
work in an earlier revision only because the workflow's drift check
ran *uncorrected* at α=0.05 while the alert used α/29 — two different
meanings of "drift" in one run, with the gate quietly passing on the
weaker one. Making the two thresholds agree exposed that the gate had
never really been satisfied on its own terms.

The retrain's justification is not V2-vs-V1. It is:

1. a persisted drift event on the production window, corrected, seven
   features, p < 1e-26, and
2. an explicit human approval,

both of them auditable rows. Re-deriving a weaker version of the same
question from the merged dataset adds no safety, and gating on it
would make the loop depend on a statistical accident.

`tests/demo/test_retraining_lifecycle.py` pins this: if that step ever
starts reporting drift, the dilution argument no longer holds and the
gate should be reconsidered rather than left off out of habit.

## Expected output

```text
============================================================
CLOSED-LOOP MLOPS DEMO
============================================================

Initial state
------------------------------------------------------------
  Dataset        : None
  Model          : None
  Model state    : NONE
  Drift status   : NORMAL
  Approval       : NONE
  Retraining     : NOT_REQUESTED
  Validation     : N/A
  Monitoring     : INACTIVE
------------------------------------------------------------
```

Drift monitoring reports statistical evidence, not a boolean:

```text
Drift monitoring
------------------------------------------------------------
  Reference           : dataset_v1 (id=1)
  Production window   : drifted window (id=3)
  Reference samples   : 8,000
  Production samples  : 1,000

  Features flagged (p < threshold):
    - amount   ks     stat=0.2365  p=3.78e-44
    - v1       ks     stat=0.2077  p=4.42e-34
    - v2       ks     stat=0.2172  p=2.94e-37
    - v3       ks     stat=0.2137  p=4.53e-36
    - v4       ks     stat=0.2109  p=4.14e-35
    - v5       ks     stat=0.1850  p=4.54e-27
    - v6       ks     stat=0.2490  p=5.95e-49

  Features not flagged: 22
    - v10      ks     stat=0.0291  p=4.32e-01
    - v11      ks     stat=0.0440  p=6.25e-02
    - v12      ks     stat=0.0377  p=1.56e-01
    - ... and 19 more

  Overall score       : 0.2490
  Threshold applied   : 1.724e-03
  Method              : scipy-backed; Bonferroni correction over 29
                        tested feature(s): alpha 0.05 -> 1.724e-03
  Status              : DRIFT DETECTED
------------------------------------------------------------
```

Seven features flagged, and they are exactly the seven the generator
shifted. Twenty-two untouched features stay quiet — which is what
makes this a *targeted covariate shift* rather than a broken feed. The
baseline window, run through identical code moments earlier, reports
`NORMAL` with all 29 features unflagged.

And it ends with the final state re-queried from the database:

```text
============================================================
FINAL SYSTEM STATE
============================================================

Dataset
------------------------------------------------------------
  dataset_v1            : 8,000 rows, id=1
  dataset_v2            : 9,000 rows, id=4 (derived from #1)
  Current version       : dataset_v2

Models
------------------------------------------------------------
  model_v1              : ARCHIVED
  model_v2              : PRODUCTION

Governance
------------------------------------------------------------
  Drift event           : drift_event_1
  Drift status          : RESOLVED
  Approval              : APPROVED
  Retraining            : COMPLETED
  Validation            : PASSED
  Monitoring            : ACTIVE
============================================================
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Full loop completed, V2 promoted |
| 3 | No drift detected — no retrain was justified |
| 4 | Admin rejected (or was unreachable) — V1 still in production |
| 5 | Retrain ran but V2 was not promoted (training or validation failed) |

Each non-zero code is a *correct* outcome of the system, not a crash.

## Reproducing the experiment

The run is deterministic given the config. To reproduce:

```bash
# 1. Bring the stack up from a clean state
docker compose --env-file .env.docker down -v
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker run --rm app alembic upgrade head

# 2. Clear previously generated data
rm -f demo/data/*.csv

# 3. Run, with the approval simulated so the run is unattended
docker compose --env-file .env.docker --profile demo run --rm \
  -e DEMO_ARGS="--mode auto --decision approve" demo
```

Controlled for you by the config: random seeds, dataset sizes, fraud
ratio, noise parameters, drift threshold and correction, readiness
bar, hyperparameters, and acceptance criteria — all printed at the
start of the run and all recorded on the persisted dataset versions.

Not controlled: XGBoost's own thread scheduling can move a metric in
the last decimal place. The *decisions* (drift detected, validation
passed) are far from their thresholds and are stable; exact metric
values may vary in the fourth decimal.

To verify determinism of the data itself:

```bash
sha256sum demo/data/dataset_v1.csv    # stable across runs
sha256sum demo/data/dataset_v2.csv    # stable across runs
```

## Failure handling

| Failure | Behaviour |
|---|---|
| Drift detection error | No retrain; the monitoring error surfaces rather than being swallowed |
| No drift detected | Loop correctly does not close — no event, no alert, no retrain (exit 3) |
| Telegram unreachable | `GovernanceEvent` records the delivery failure; drift event untouched; decision defaults to **deny** |
| Admin rejects | No Dataset V2, no training run, no model version; V1 stays in production (exit 4) |
| Training fails | `TRAINING_FAILED` event; V2 never registered; V1 stays in production (exit 5) |
| Validation fails | V2 marked `REJECTED` and audited; V1 stays in production (exit 5) |

In every row, the production model is either correctly replaced or
left exactly as it was.

## Tests

The happy path needs Airflow and MLflow, so CI cannot execute it. What
CI does verify is every decision the demo makes, against a real
database with a local subprocess orchestrator substituted for Airflow:

```bash
pytest tests/demo/ -v
```

| File | Covers |
|---|---|
| `test_drift_detection.py` | Baseline is *not* flagged; drifted is; flagged set ⊆ injected set; evaluation + `CRITICAL` event persisted; noise parameters recorded and byte-identical on replay |
| `test_dataset_v2.py` | V2 contains V1's rows in order; is not a regenerated replacement; `parent_version_id` set; `derived_from` edge in the lineage graph; unknown parent rejected; idempotent |
| `test_approval.py` | Approve / reject / unreachable paths; audit rows link drift event → decision; alert leaks no secrets; `RecordedDecisionGate` replays denials too |
| `test_retraining_lifecycle.py` | V2 promoted and V1 archived; exactly one `PRODUCTION` version; **and** that denial, training failure, and validation failure each leave V1 in production |
| `test_reproducibility.py` | Config is frozen and complete; the environment cannot move seeds or thresholds; phases receive the configured values |

The framework changes this demo required are covered alongside the
existing suites — `tests/unit/test_lineage.py`,
`tests/unit/test_approval_gate.py`, `tests/unit/test_drift.py`,
`tests/unit/test_migrations.py`.
