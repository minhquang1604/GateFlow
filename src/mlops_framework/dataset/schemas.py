"""Dataset schemas using Pydantic."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DatasetCreate(BaseModel):
    """Schema for creating a new dataset."""

    name: str = Field(..., min_length=1, max_length=255, description="Unique dataset name")
    description: str | None = Field(None, description="Dataset description")


class DatasetUpdate(BaseModel):
    """Schema for updating a dataset."""

    name: str | None = Field(None, min_length=1, max_length=255, description="Unique dataset name")
    description: str | None = Field(None, description="Dataset description")


class DatasetResponse(BaseModel):
    """Schema for dataset response."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Dataset ID")
    name: str = Field(..., description="Dataset name")
    description: str | None = Field(None, description="Dataset description")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")


class DatasetVersionCreate(BaseModel):
    """Schema for creating a new dataset version."""

    storage_uri: str = Field(..., min_length=1, max_length=512, description="URI to the dataset storage")
    row_count: int = Field(..., ge=0, description="Number of rows in this version")
    metadata: dict[str, Any] | None = Field(default_factory=dict, description="Additional metadata")


class DatasetVersionResponse(BaseModel):
    """Schema for dataset version response."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Dataset version ID")
    dataset_id: int = Field(..., description="Parent dataset ID")
    version_number: int = Field(..., description="Version number")
    storage_uri: str = Field(..., description="URI to the dataset storage")
    checksum: str = Field(..., description="SHA-256 checksum of the data")
    schema_hash: str = Field(..., description="SHA-256 hash of the schema")
    row_count: int = Field(..., description="Number of rows")
    metadata_json: str | None = Field(None, description="JSON-encoded metadata")
    is_immutable: bool = Field(..., description="Whether this version is immutable")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")


class DatasetVersionMetadata(BaseModel):
    """Schema for dataset version metadata parsed from JSON."""

    source_checksum: str | None = Field(None, description="Checksum of source data")
    source_schema_hash: str | None = Field(None, description="Schema hash of source data")
    columns: list[dict[str, str]] | None = Field(None, description="Column definitions")
    extra: dict[str, Any] | None = Field(default_factory=dict, description="Extra metadata")
