"""Deterministic schema hashing for datasets."""

import hashlib
import json
from typing import Any, Sequence


class ColumnSpec:
    """Specification for a data column."""

    def __init__(self, name: str, dtype: str) -> None:
        """Initialize column specification.

        Args:
            name: Column name
            dtype: Data type (e.g., 'int64', 'float64', 'object', 'datetime64')
        """
        self.name = name
        self.dtype = dtype

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary representation."""
        return {"name": self.name, "dtype": self.dtype}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ColumnSpec):
            return False
        return self.name == other.name and self.dtype == other.dtype

    def __hash__(self) -> int:
        return hash((self.name, self.dtype))


def calculate_schema_hash(columns: Sequence[ColumnSpec | dict[str, str]]) -> str:
    """Calculate deterministic SHA-256 hash of a schema.

    The schema is represented as a canonical JSON string with:
    - Columns ordered by name
    - Each column represented as {"name": ..., "dtype": ...}

    Args:
        columns: Sequence of ColumnSpec or dicts with 'name' and 'dtype' keys

    Returns:
        str: SHA-256 hex digest (64 characters)
    """
    # Convert to ColumnSpec if needed and sort by name for determinism
    column_specs: list[ColumnSpec] = []
    for col in columns:
        if isinstance(col, ColumnSpec):
            column_specs.append(col)
        elif isinstance(col, dict):
            column_specs.append(ColumnSpec(name=col["name"], dtype=col["dtype"]))
        else:
            raise ValueError(f"Invalid column specification: {col}")

    # Sort by column name for deterministic ordering
    column_specs.sort(key=lambda c: c.name)

    # Convert to canonical JSON representation
    columns_json = json.dumps(
        [col.to_dict() for col in column_specs],
        separators=(",", ":"),
    )

    return hashlib.sha256(columns_json.encode("utf-8")).hexdigest()


def calculate_pandas_schema_hash(df: Any) -> str:
    """Calculate deterministic schema hash from a pandas DataFrame.

    Args:
        df: pandas DataFrame

    Returns:
        str: SHA-256 hex digest (64 characters)
    """
    try:
        import pandas as pd  # type: ignore[import]
    except ImportError:
        raise ImportError("pandas is required for pandas schema hashing")

    if not isinstance(df, pd.DataFrame):
        raise ValueError("df must be a pandas DataFrame")

    columns = [
        ColumnSpec(name=str(name), dtype=str(dtype))
        for name, dtype in zip(df.columns, df.dtypes)
    ]
    return calculate_schema_hash(columns)


def calculate_dict_schema_hash(schema: dict[str, str]) -> str:
    """Calculate deterministic schema hash from a dictionary.

    The dictionary maps column names to data types.

    Args:
        schema: Dictionary mapping column names to data types

    Returns:
        str: SHA-256 hex digest (64 characters)
    """
    columns = [ColumnSpec(name=name, dtype=dtype) for name, dtype in schema.items()]
    return calculate_schema_hash(columns)


def verify_schema_hash(columns: Sequence[ColumnSpec | dict[str, str]], expected_hash: str) -> bool:
    """Verify that schema matches expected hash.

    Args:
        columns: Column specifications
        expected_hash: Expected SHA-256 hex digest

    Returns:
        bool: True if hash matches
    """
    actual_hash = calculate_schema_hash(columns)
    return actual_hash == expected_hash
