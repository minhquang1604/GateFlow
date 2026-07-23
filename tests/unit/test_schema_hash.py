"""Unit tests for schema hashing module."""

import pytest
from mlops_framework.dataset.versioning import (
    ColumnSpec,
    calculate_schema_hash,
    calculate_dict_schema_hash,
    verify_schema_hash,
)


class TestColumnSpec:
    """Tests for ColumnSpec class."""

    def test_column_spec_creation(self):
        """Test creating a ColumnSpec."""
        spec = ColumnSpec(name="age", dtype="int64")
        assert spec.name == "age"
        assert spec.dtype == "int64"

    def test_column_spec_equality(self):
        """Test ColumnSpec equality."""
        spec1 = ColumnSpec(name="age", dtype="int64")
        spec2 = ColumnSpec(name="age", dtype="int64")
        spec3 = ColumnSpec(name="name", dtype="int64")
        assert spec1 == spec2
        assert spec1 != spec3

    def test_column_spec_hash(self):
        """Test ColumnSpec hash."""
        spec1 = ColumnSpec(name="age", dtype="int64")
        spec2 = ColumnSpec(name="age", dtype="int64")
        assert hash(spec1) == hash(spec2)


class TestCalculateSchemaHash:
    """Tests for calculate_schema_hash function."""

    def test_schema_hash_deterministic(self):
        """Test that schema hash is deterministic."""
        columns = [
            ColumnSpec(name="id", dtype="int64"),
            ColumnSpec(name="name", dtype="object"),
            ColumnSpec(name="age", dtype="int64"),
        ]
        result1 = calculate_schema_hash(columns)
        result2 = calculate_schema_hash(columns)
        assert result1 == result2

    def test_schema_hash_order_independent(self):
        """Test that schema hash is independent of column order."""
        columns1 = [
            ColumnSpec(name="age", dtype="int64"),
            ColumnSpec(name="id", dtype="int64"),
        ]
        columns2 = [
            ColumnSpec(name="id", dtype="int64"),
            ColumnSpec(name="age", dtype="int64"),
        ]
        # Sorted alphabetically, so both should produce the same hash
        assert calculate_schema_hash(columns1) == calculate_schema_hash(columns2)

    def test_schema_hash_different_columns(self):
        """Test that different columns produce different hashes."""
        columns1 = [ColumnSpec(name="id", dtype="int64")]
        columns2 = [ColumnSpec(name="name", dtype="object")]
        assert calculate_schema_hash(columns1) != calculate_schema_hash(columns2)

    def test_schema_hash_different_dtypes(self):
        """Test that different data types produce different hashes."""
        columns1 = [ColumnSpec(name="age", dtype="int64")]
        columns2 = [ColumnSpec(name="age", dtype="float64")]
        assert calculate_schema_hash(columns1) != calculate_schema_hash(columns2)

    def test_schema_hash_length(self):
        """Test that SHA-256 hash is 64 hex characters."""
        columns = [ColumnSpec(name="id", dtype="int64")]
        result = calculate_schema_hash(columns)
        assert len(result) == 64

    def test_schema_hash_with_dict_input(self):
        """Test schema hash with dictionary input."""
        columns = [
            {"name": "id", "dtype": "int64"},
            {"name": "name", "dtype": "object"},
        ]
        result = calculate_schema_hash(columns)
        assert len(result) == 64


class TestCalculateDictSchemaHash:
    """Tests for calculate_dict_schema_hash function."""

    def test_dict_schema_hash(self):
        """Test schema hash from dictionary."""
        schema = {"id": "int64", "name": "object"}
        result = calculate_dict_schema_hash(schema)
        assert len(result) == 64

    def test_dict_schema_hash_deterministic(self):
        """Test dict schema hash is deterministic."""
        schema = {"id": "int64", "name": "object"}
        result1 = calculate_dict_schema_hash(schema)
        result2 = calculate_dict_schema_hash(schema)
        assert result1 == result2


class TestVerifySchemaHash:
    """Tests for verify_schema_hash function."""

    def test_verify_schema_hash_valid(self):
        """Test schema hash verification with valid hash."""
        columns = [ColumnSpec(name="id", dtype="int64")]
        expected_hash = calculate_schema_hash(columns)
        assert verify_schema_hash(columns, expected_hash) is True

    def test_verify_schema_hash_invalid(self):
        """Test schema hash verification with invalid hash."""
        columns = [ColumnSpec(name="id", dtype="int64")]
        wrong_hash = "0" * 64
        assert verify_schema_hash(columns, wrong_hash) is False
