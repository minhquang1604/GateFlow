"""Pipeline registry — maps friendly pipeline names to orchestrator-executable IDs.

A *pipeline* in the MLOps framework is anything an :class:`Orchestrator` can run.
Different orchestrators accept different identifiers:

- ``LocalDockerOrchestrator`` accepts a Python ``module:function`` reference.
- ``AirflowOrchestrator`` accepts a DAG id.

App developers want to refer to pipelines by a stable, friendly name (e.g.
``"xgboost-training"``) — independently of where the pipeline is implemented
or how the orchestrator references it. ``PipelineRegistry`` is that mapping.

It is intentionally simple: an in-memory dict with explicit register / resolve
operations. Persistence, versioning, and validation are not part of Week 4
and can be added later without changing the public surface.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


class PipelineNotFoundError(KeyError):
    """Raised when a friendly name is not registered.

    Inherits from :class:`KeyError` so existing ``except KeyError`` blocks
    continue to work, but the type is exported for precise catching.
    """


@dataclass
class PipelineEntry:
    """A single registered pipeline.

    Attributes:
        name: Friendly, app-facing name (e.g. ``"xgboost-training"``).
        pipeline_id: Orchestrator-executable identifier (DAG id or
            ``module:function`` reference).
        description: Optional human-readable description.
        parameters: Optional default parameter values. These are exposed
            by the SDK so app developers can see what a pipeline expects.
    """

    name: str
    pipeline_id: str
    description: str = ""
    parameters: dict[str, object] = field(default_factory=dict)


class PipelineRegistry:
    """In-memory registry of named pipelines.

    Example::

        registry = PipelineRegistry()
        registry.register(
            PipelineEntry(
                name="xgboost-training",
                pipeline_id="my_pkg.pipelines.train_xgb:main",
                description="XGBoost trainer for tabular data",
                parameters={"max_depth": 6, "learning_rate": 0.1},
            )
        )
        registry.resolve("xgboost-training")
        # -> "my_pkg.pipelines.train_xgb:main"
    """

    def __init__(self, entries: Iterable[PipelineEntry] | None = None) -> None:
        self._entries: dict[str, PipelineEntry] = {}
        if entries:
            for entry in entries:
                self.register(entry)

    def register(self, entry: PipelineEntry) -> None:
        """Register a pipeline. Overwrites if the name already exists."""
        self._entries[entry.name] = entry

    def register_many(self, entries: Iterable[PipelineEntry]) -> None:
        """Register multiple pipelines at once."""
        for entry in entries:
            self.register(entry)

    def resolve(self, name: str) -> str:
        """Return the orchestrator-executable id for ``name``.

        Raises:
            PipelineNotFoundError: if the name is not registered.
        """
        try:
            return self._entries[name].pipeline_id
        except KeyError as exc:
            raise PipelineNotFoundError(
                f"Pipeline {name!r} is not registered. "
                f"Known pipelines: {sorted(self._entries)}"
            ) from exc

    def get(self, name: str) -> PipelineEntry:
        """Return the full :class:`PipelineEntry` for ``name``."""
        try:
            return self._entries[name]
        except KeyError as exc:
            raise PipelineNotFoundError(
                f"Pipeline {name!r} is not registered."
            ) from exc

    def names(self) -> list[str]:
        """Return all registered pipeline names (sorted)."""
        return sorted(self._entries)

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)
