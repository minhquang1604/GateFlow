"""Deterministic checksum calculation for dataset content."""

import hashlib
import json
from pathlib import Path
from typing import Any

from mlops_framework.exceptions import ChecksumError


def calculate_checksum(data: bytes) -> str:
    """Calculate SHA-256 checksum of data.

    Args:
        data: Raw data bytes

    Returns:
        str: SHA-256 hex digest (64 characters)
    """
    return hashlib.sha256(data).hexdigest()


def calculate_file_checksum(file_path: str | Path) -> str:
    """Calculate SHA-256 checksum of a file.

    Args:
        file_path: Path to the file

    Returns:
        str: SHA-256 hex digest (64 characters)

    Raises:
        ChecksumError: If the file cannot be read
    """
    path = Path(file_path)
    if not path.exists():
        raise ChecksumError(f"File not found: {file_path}")

    try:
        sha256_hash = hashlib.sha256()
        with open(path, "rb") as f:
            # Read in chunks to handle large files
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()
    except Exception as e:
        raise ChecksumError(f"Failed to calculate checksum for {file_path}: {e}") from e


def calculate_dict_checksum(data: dict[str, Any]) -> str:
    """Calculate deterministic checksum of a dictionary.

    The dictionary is sorted by keys to ensure deterministic output.

    Args:
        data: Dictionary to checksum

    Returns:
        str: SHA-256 hex digest (64 characters)
    """
    # Sort keys for deterministic output
    json_str = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return calculate_checksum(json_str.encode("utf-8"))


def verify_checksum(data: bytes, expected_checksum: str) -> bool:
    """Verify that data matches expected checksum.

    Args:
        data: Raw data bytes
        expected_checksum: Expected SHA-256 hex digest

    Returns:
        bool: True if checksum matches
    """
    actual_checksum = calculate_checksum(data)
    return actual_checksum == expected_checksum
