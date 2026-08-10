"""Fraud-detection data: the real Kaggle CSV, plus a synthetic stand-in.

Two sources, one column contract:

* ``creditcard.csv`` — the real `Kaggle Credit Card Fraud Detection
  <https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud>`_ dataset:
  284,807 transactions over 48 hours, 492 of them fraudulent (0.17%).
  144 MB, so it is downloaded rather than committed — see this package's
  README. Its header is ``Time,V1..V28,Amount,Class``.
* :func:`generate` / :func:`write_csv` — a small synthetic CSV with the
  same shape, so the case study's tests stay hermetic and fast.

The two differ only in capitalisation and column order, so everything
downstream works against the canonical lower-case names below and
:func:`normalize_columns` maps either source onto them. Doing it here
rather than in the pipeline means the readiness engine's schema hash is
stable across both sources.

Canonical columns:
    time        — seconds since the first transaction
    amount      — transaction amount (USD)
    v1..v28     — anonymised PCA features
    class       — 0 (legit) or 1 (fraud)
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Any, Iterable, Optional

#: The canonical column order the framework registers and the pipeline reads.
CANONICAL_COLUMNS: list[str] = (
    ["time", "amount"] + [f"v{i}" for i in range(1, 29)] + ["class"]
)

#: Filename of the real dataset, relative to this package's ``data/``.
REAL_DATASET_FILENAME = "creditcard.csv"


#: V-features whose *legit*-population distribution moves under drift.
#: A subset, not all 28 — enough for the KS test to flag real drift
#: without erasing every feature's separating signal at once.
_DRIFT_FEATURES: frozenset[int] = frozenset(range(1, 7))


def generate(
    n_rows: int = 5000,
    fraud_ratio: float = 0.002,
    seed: int = 42,
    drift_shift: float = 0.0,
) -> Iterable[dict]:
    """Yield rows of synthetic fraud-detection data.

    ``drift_shift`` simulates a covariate shift in *legitimate* traffic —
    e.g. a new payment processor or a seasonal spending change — without
    touching how fraud itself looks. At ``drift_shift=0`` (the default)
    this is byte-for-byte what the function has always produced.
    Legit ``amount`` scales up by ``(1 + drift_shift)`` and legit rows'
    low-index V-features (:data:`_DRIFT_FEATURES`) drift toward the
    fraud cluster. A decision boundary fit at ``drift_shift=0`` sees a
    rising false-positive rate as the shift grows; a boundary refit on
    the shifted data recovers, because fraud and (shifted) legit are
    still separable — only the boundary needs to move.
    """
    rng = random.Random(seed)
    columns = ["time", "amount"] + [f"v{i}" for i in range(1, 29)] + ["class"]
    n_fraud = max(1, int(n_rows * fraud_ratio))
    fraud_indices = set(rng.sample(range(n_rows), n_fraud))

    for i in range(n_rows):
        is_fraud = i in fraud_indices
        # Legit transactions are small and well-distributed; fraudulent
        # transactions are larger and clustered.
        if is_fraud:
            amount = round(rng.uniform(80, 2000), 2)
        else:
            amount = round(rng.expovariate(1 / 60) * (1 + drift_shift), 2)
        row = {
            "time": i * 1.0,
            "amount": amount,
        }
        for j in range(1, 29):
            # Fraud rows have slightly shifted distributions so a model
            # could learn them in principle.
            base = rng.gauss(0, 1)
            if is_fraud:
                base += rng.gauss(0.5, 0.3)
            elif drift_shift and j in _DRIFT_FEATURES:
                base += rng.gauss(drift_shift * 0.5, 0.15)
            row[f"v{j}"] = round(base, 4)
        row["class"] = 1 if is_fraud else 0
        yield row


def write_csv(
    path: str | Path,
    n_rows: int = 5000,
    fraud_ratio: float = 0.002,
    seed: int = 42,
    drift_shift: float = 0.0,
) -> Path:
    """Generate and write a CSV file. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    columns = ["time", "amount"] + [f"v{i}" for i in range(1, 29)] + ["class"]
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for row in generate(
            n_rows=n_rows, fraud_ratio=fraud_ratio, seed=seed, drift_shift=drift_shift
        ):
            w.writerow(row)
    return p


def schema_metadata() -> dict:
    """Return the dataset-version metadata for fraud data.

    The framework's readiness engine uses this to verify column presence
    and dtypes.
    """
    columns = [{"name": "time", "dtype": "float64"}]
    columns += [{"name": "amount", "dtype": "float64"}]
    for i in range(1, 29):
        columns.append({"name": f"v{i}", "dtype": "float64"})
    columns.append({"name": "class", "dtype": "int64"})
    return {"columns": columns}


def feature_columns() -> list[str]:
    """Return the feature columns used by ``train_xgboost``."""
    return ["time", "amount"] + [f"v{i}" for i in range(1, 29)]


def target_column() -> str:
    """Return the target column used by ``train_xgboost``."""
    return "class"


def normalize_columns(df: Any) -> Any:
    """Rename either source's columns onto :data:`CANONICAL_COLUMNS`.

    The real Kaggle file ships ``Time,V1..V28,Amount,Class``; the
    synthetic generator writes ``time,amount,v1..v28,class``. Lower-casing
    reconciles them, and reindexing puts the columns in canonical order so
    the schema hash does not depend on which source produced the file.

    Raises:
        ValueError: if a canonical column is missing after renaming —
            better to fail loudly here than to train on a silently
            mis-aligned feature matrix.
    """
    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"CSV is missing expected fraud columns {missing}; "
            f"got {sorted(df.columns)}"
        )
    return df[CANONICAL_COLUMNS]


def to_dataframe(path: str | Path) -> Any:
    """Load a fraud CSV into a canonical ``pandas.DataFrame``.

    Accepts either data source and returns :data:`CANONICAL_COLUMNS` in
    order. The training pipeline imports this lazily so the case study
    remains importable without ``pandas`` installed for unit tests.
    """
    import pandas as pd  # type: ignore[import-not-found]

    return normalize_columns(pd.read_csv(path))


def describe_csv(path: str | Path) -> dict:
    """Profile a fraud CSV for registration as a DatasetVersion.

    Returns the row count and the metadata blob the framework's readiness
    engine reads — the observed dtypes, not the declared ones, so a file
    whose columns arrive as strings is caught at registration rather than
    at ``model.fit``.

    Returns:
        ``{"row_count": int, "metadata": {"columns": [...], ...}}``
    """
    df = to_dataframe(path)
    columns = [
        {"name": str(name), "dtype": str(dtype)}
        for name, dtype in zip(df.columns, df.dtypes)
    ]
    n_fraud = int(df["class"].sum())
    n_rows = int(len(df))
    return {
        "row_count": n_rows,
        "metadata": {
            "columns": columns,
            "source": Path(path).name,
            "target": target_column(),
            "n_fraud": n_fraud,
            "fraud_ratio": round(n_fraud / n_rows, 6) if n_rows else 0.0,
            "missing_values": int(df.isna().sum().sum()),
        },
    }