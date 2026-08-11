"""Dataset manager for managing datasets and versions."""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.dataset.checksum import calculate_dict_checksum
from mlops_framework.dataset.versioning import calculate_schema_hash
from mlops_framework.exceptions import (
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    DuplicateDatasetNameError,
    ImmutableDatasetVersionError,
)


class DatasetManager:
    """Manages datasets and their versions.

    This manager provides business logic for:
    - Creating and managing datasets
    - Creating immutable dataset versions
    - Calculating checksums and schema hashes
    - Enforcing version immutability
    """

    def __init__(self, session: Session) -> None:
        """Initialize dataset manager.

        Args:
            session: SQLAlchemy session
        """
        self._session = session

    def create_dataset(self, name: str, description: str | None = None) -> Dataset:
        """Create a new dataset.

        Args:
            name: Unique dataset name
            description: Optional dataset description

        Returns:
            Dataset: Created dataset

        Raises:
            DuplicateDatasetNameError: If a dataset with this name already exists
        """
        # Check for duplicate name
        existing = self._session.execute(
            select(Dataset).where(Dataset.name == name)
        ).scalar_one_or_none()

        if existing is not None:
            raise DuplicateDatasetNameError(f"Dataset with name '{name}' already exists")

        dataset = Dataset(name=name, description=description)
        self._session.add(dataset)
        self._session.flush()  # Get the ID without committing

        return dataset

    def get_dataset(self, dataset_id: int) -> Dataset:
        """Get a dataset by ID.

        Args:
            dataset_id: Dataset ID

        Returns:
            Dataset: The dataset

        Raises:
            DatasetNotFoundError: If dataset not found
        """
        dataset = self._session.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset with id {dataset_id} not found")
        return dataset

    def get_dataset_by_name(self, name: str) -> Dataset | None:
        """Get a dataset by name.

        Args:
            name: Dataset name

        Returns:
            Optional[Dataset]: The dataset if found
        """
        return self._session.execute(
            select(Dataset).where(Dataset.name == name)
        ).scalar_one_or_none()

    def list_datasets(self) -> list[Dataset]:
        """List all datasets.

        Returns:
            list[Dataset]: List of all datasets
        """
        return list(self._session.execute(select(Dataset)).scalars().all())

    def _get_next_version_number(self, dataset_id: int) -> int:
        """Get the next sequential version number for a dataset.

        Args:
            dataset_id: Dataset ID

        Returns:
            int: Next version number (starting from 1)
        """
        result = self._session.execute(
            select(func.max(DatasetVersion.version_number))
            .where(DatasetVersion.dataset_id == dataset_id)
        ).scalar()

        return (result or 0) + 1

    def _calculate_version_checksum(
        self,
        storage_uri: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Calculate checksum for a dataset version.

        The checksum is calculated from the storage URI and metadata to ensure
        reproducibility. In a real implementation, this could also calculate
        a hash of the actual data files.

        Args:
            storage_uri: URI to the dataset storage
            metadata: Optional metadata dictionary

        Returns:
            str: SHA-256 checksum
        """
        data = {
            "storage_uri": storage_uri,
            "metadata": metadata or {},
        }
        return calculate_dict_checksum(data)

    def _calculate_version_schema_hash(
        self,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Calculate schema hash for a dataset version.

        The schema hash is extracted from version metadata if available,
        otherwise computed from the metadata dictionary.

        Args:
            metadata: Optional metadata dictionary containing schema info

        Returns:
            str: SHA-256 schema hash
        """
        if metadata and "columns" in metadata:
            return calculate_schema_hash(metadata["columns"])

        # If no explicit columns, use the entire metadata for hashing
        if metadata:
            return calculate_dict_checksum({"schema": metadata})

        # Default empty schema hash
        return calculate_schema_hash([])

    def create_version(
        self,
        dataset_id: int,
        storage_uri: str,
        row_count: int,
        metadata: dict[str, Any] | None = None,
    ) -> DatasetVersion:
        """Create a new version for a dataset.

        The version is immutable once created - this ensures reproducibility
        of training runs.

        Args:
            dataset_id: Parent dataset ID
            storage_uri: URI to the dataset storage
            row_count: Number of rows in this version
            metadata: Optional metadata (can include 'columns' for schema)

        Returns:
            DatasetVersion: Created version

        Raises:
            DatasetNotFoundError: If dataset not found
        """
        # Verify dataset exists
        self.get_dataset(dataset_id)

        # Get next version number
        version_number = self._get_next_version_number(dataset_id)

        # Calculate checksum and schema hash
        checksum = self._calculate_version_checksum(storage_uri, metadata)
        schema_hash = self._calculate_version_schema_hash(metadata)

        # Create version
        version = DatasetVersion(
            dataset_id=dataset_id,
            version_number=version_number,
            storage_uri=storage_uri,
            checksum=checksum,
            schema_hash=schema_hash,
            row_count=row_count,
            metadata_json=json.dumps(metadata) if metadata else None,
            is_immutable=True,
        )

        self._session.add(version)
        self._session.flush()

        return version

    def get_version(self, version_id: int) -> DatasetVersion:
        """Get a dataset version by ID.

        Args:
            version_id: Version ID

        Returns:
            DatasetVersion: The version

        Raises:
            DatasetVersionNotFoundError: If version not found
        """
        version = self._session.get(DatasetVersion, version_id)
        if version is None:
            raise DatasetVersionNotFoundError(f"DatasetVersion with id {version_id} not found")
        return version

    def list_versions(self, dataset_id: int) -> list[DatasetVersion]:
        """List all versions for a dataset.

        Args:
            dataset_id: Dataset ID

        Returns:
            list[DatasetVersion]: List of versions, ordered by version number
        """
        return list(
            self._session.execute(
                select(DatasetVersion)
                .where(DatasetVersion.dataset_id == dataset_id)
                .order_by(DatasetVersion.version_number)
            ).scalars().all()
        )

    def get_latest_version(self, dataset_id: int) -> DatasetVersion | None:
        """Get the latest version for a dataset.

        Args:
            dataset_id: Dataset ID

        Returns:
            Optional[DatasetVersion]: The latest version or None if no versions exist
        """
        return self._session.execute(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id == dataset_id)
            .order_by(DatasetVersion.version_number.desc())
        ).scalars().first()

    def _ensure_mutable(self, version: DatasetVersion) -> None:
        """Ensure a version is mutable (for internal use).

        Raises:
            ImmutableDatasetVersionError: If version is immutable
        """
        if version.is_immutable:
            raise ImmutableDatasetVersionError(
                f"DatasetVersion {version.id} is immutable and cannot be modified"
            )

    def get_version_metadata(self, version_id: int) -> dict[str, Any]:
        """Get parsed metadata for a dataset version.

        Args:
            version_id: Version ID

        Returns:
            dict[str, Any]: Parsed metadata

        Raises:
            DatasetVersionNotFoundError: If version not found
        """
        version = self.get_version(version_id)
        if version.metadata_json:
            return json.loads(version.metadata_json)
        return {}

    def compare_versions(self, version_id_1: int, version_id_2: int) -> dict[str, Any]:
        """Compare two dataset versions at metadata level.

        Args:
            version_id_1: First version ID
            version_id_2: Second version ID

        Returns:
            dict[str, Any]: Comparison result with differences
        """
        v1 = self.get_version(version_id_1)
        v2 = self.get_version(version_id_2)

        return {
            "version_1": {
                "id": v1.id,
                "version_number": v1.version_number,
                "checksum": v1.checksum,
                "schema_hash": v1.schema_hash,
                "row_count": v1.row_count,
            },
            "version_2": {
                "id": v2.id,
                "version_number": v2.version_number,
                "checksum": v2.checksum,
                "schema_hash": v2.schema_hash,
                "row_count": v2.row_count,
            },
            "differences": {
                "checksum_changed": v1.checksum != v2.checksum,
                "schema_changed": v1.schema_hash != v2.schema_hash,
                "row_count_changed": v1.row_count != v2.row_count,
            },
        }
