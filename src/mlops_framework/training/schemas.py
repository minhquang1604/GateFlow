"""Training schemas using Pydantic."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TrainingRunCreate(BaseModel):
    """Schema for creating a new training run."""

    dataset_version_id: int = Field(..., description="ID of the dataset version to train on")
    trigger_type: str = Field(default="MANUAL", description="Trigger type for the run")
    metadata: dict[str, Any] | None = Field(default_factory=dict, description="Additional metadata")


class TrainingRunUpdate(BaseModel):
    """Schema for updating a training run."""

    status: str | None = Field(None, description="Run status")
    started_at: datetime | None = Field(None, description="Start timestamp")
    completed_at: datetime | None = Field(None, description="Completion timestamp")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")


class TrainingRunResponse(BaseModel):
    """Schema for training run response."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Training run ID")
    dataset_version_id: int = Field(..., description="Dataset version ID")
    status: str = Field(..., description="Run status")
    trigger_type: str = Field(..., description="Trigger type")
    started_at: datetime | None = Field(None, description="Start timestamp")
    completed_at: datetime | None = Field(None, description="Completion timestamp")
    metadata_json: str | None = Field(None, description="JSON-encoded metadata")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
