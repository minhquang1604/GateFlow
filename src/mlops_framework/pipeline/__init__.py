"""Pipeline package — registry of named pipelines."""

from mlops_framework.pipeline.manager import (
    PipelineEntry,
    PipelineNotFoundError,
    PipelineRegistry,
)

__all__ = ["PipelineEntry", "PipelineNotFoundError", "PipelineRegistry"]
