"""Unit tests for checksum module."""

import pytest
from mlops_framework.dataset.checksum import (
    calculate_checksum,
    calculate_file_checksum,
    calculate_dict_checksum,
    verify_checksum,
)
from mlops_framework.exceptions import ChecksumError


class TestCalculateChecksum:
    """Tests for calculate_checksum function."""

    def test_checksum_deterministic(self):
        """Test that checksum is deterministic."""
        data = b"test data"
        result1 = calculate_checksum(data)
        result2 = calculate_checksum(data)
        assert result1 == result2

    def test_checksum_different_data(self):
        """Test that different data produces different checksums."""
        data1 = b"test data 1"
        data2 = b"test data 2"
        assert calculate_checksum(data1) != calculate_checksum(data2)

    def test_checksum_length(self):
        """Test that SHA-256 checksum is 64 hex characters."""
        data = b"test data"
        result = calculate_checksum(data)
        assert len(result) == 64
        assert result.isalnum()


class TestCalculateDictChecksum:
    """Tests for calculate_dict_checksum function."""

    def test_dict_checksum_deterministic(self):
        """Test that dict checksum is deterministic."""
        data = {"key1": "value1", "key2": "value2"}
        result1 = calculate_dict_checksum(data)
        result2 = calculate_dict_checksum(data)
        assert result1 == result2

    def test_dict_checksum_key_order_independent(self):
        """Test that dict checksum is independent of key order."""
        data1 = {"a": "1", "b": "2"}
        data2 = {"b": "2", "a": "1"}
        assert calculate_dict_checksum(data1) == calculate_dict_checksum(data2)


class TestVerifyChecksum:
    """Tests for verify_checksum function."""

    def test_verify_checksum_valid(self):
        """Test checksum verification with valid checksum."""
        data = b"test data"
        checksum = calculate_checksum(data)
        assert verify_checksum(data, checksum) is True

    def test_verify_checksum_invalid(self):
        """Test checksum verification with invalid checksum."""
        data = b"test data"
        wrong_checksum = "0" * 64
        assert verify_checksum(data, wrong_checksum) is False
