"""Synthetic fraud-detection data generator.

The Kaggle credit-card-fraud dataset is famously imbalanced. We don't ship
the real data; we generate a small CSV that matches its column shape so the
case study is fully self-contained and reproducible.

Output columns:
    time        — seconds since the first transaction
    amount      — transaction amount (USD)
    v1..v28     — anonymised PCA features (the real dataset has 28)
    class       — 0 (legit) or 1 (fraud)

The class ratio is fixed at 0.2% fraud, matching the public dataset.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Any, Iterable, Optional


def generate(
    n_rows: int = 5000,
    fraud_ratio: float = 0.002,
    seed: int = 42,
) -> Iterable[dict]:
    """Yield rows of synthetic fraud-detection data."""
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
            amount = round(rng.expovariate(1 / 60), 2)
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
            row[f"v{j}"] = round(base, 4)
        row["class"] = 1 if is_fraud else 0
        yield row


def write_csv(
    path: str | Path,
    n_rows: int = 5000,
    fraud_ratio: float = 0.002,
    seed: int = 42,
) -> Path:
    """Generate and write a CSV file. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    columns = ["time", "amount"] + [f"v{i}" for i in range(1, 29)] + ["class"]
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for row in generate(n_rows=n_rows, fraud_ratio=fraud_ratio, seed=seed):
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


def to_dataframe(path: str | Path) -> Any:
    """Load the fraud CSV into a ``pandas.DataFrame``.

    The training pipeline imports this lazily so the case study remains
    importable without ``pandas`` installed for unit tests.
    """
    import pandas as pd  # type: ignore[import-not-found]

    df = pd.read_csv(path)
    return df