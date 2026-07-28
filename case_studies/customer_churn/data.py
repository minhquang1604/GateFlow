"""Synthetic customer-churn data generator.

The real Telco Customer Churn dataset is hosted on Kaggle. We don't ship
it; we generate rows that match its shape so the case study is fully
self-contained.

Columns:
    customer_id    — synthetic unique id
    tenure_months  — months the customer has been with the company
    monthly_charges — current monthly bill (USD)
    contract_type  — "month-to-month" | "one-year" | "two-year"
    payment_method — "credit-card" | "bank-transfer" | "electronic-check"
    num_complaints — int 0..10
    churn          — 0 (stayed) | 1 (churned)

The churn rate is approximately 27%, matching the public dataset.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Iterable


_CONTRACT_TYPES = ("month-to-month", "one-year", "two-year")
_PAYMENT_METHODS = ("credit-card", "bank-transfer", "electronic-check")


def generate(n_rows: int = 3000, churn_rate: float = 0.27, seed: int = 7) -> Iterable[dict]:
    rng = random.Random(seed)
    for i in range(n_rows):
        contract = rng.choice(_CONTRACT_TYPES)
        # Month-to-month customers churn much more often.
        if contract == "month-to-month":
            p_churn = 0.45
        elif contract == "one-year":
            p_churn = 0.15
        else:
            p_churn = 0.05
        churn = 1 if rng.random() < p_churn else 0
        yield {
            "customer_id": f"C{i:06d}",
            "tenure_months": rng.randint(1, 72),
            "monthly_charges": round(rng.uniform(20, 120), 2),
            "contract_type": contract,
            "payment_method": rng.choice(_PAYMENT_METHODS),
            "num_complaints": rng.choices(range(0, 6), weights=[70, 15, 8, 4, 2, 1])[0],
            "churn": churn,
        }


def write_csv(
    path: str | Path,
    n_rows: int = 3000,
    churn_rate: float = 0.27,
    seed: int = 7,
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "customer_id", "tenure_months", "monthly_charges",
        "contract_type", "payment_method", "num_complaints", "churn",
    ]
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for row in generate(n_rows=n_rows, churn_rate=churn_rate, seed=seed):
            w.writerow(row)
    return p


def schema_metadata() -> dict:
    """Return dataset-version metadata for the churn data.

    Categorical columns are included as dtype=object so the readiness
    engine can verify they are present.
    """
    return {
        "columns": [
            {"name": "customer_id", "dtype": "object"},
            {"name": "tenure_months", "dtype": "int64"},
            {"name": "monthly_charges", "dtype": "float64"},
            {"name": "contract_type", "dtype": "object"},
            {"name": "payment_method", "dtype": "object"},
            {"name": "num_complaints", "dtype": "int64"},
            {"name": "churn", "dtype": "int64"},
        ]
    }
