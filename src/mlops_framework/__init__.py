"""MLOps Framework package."""

from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.training.manager import TrainingManager

__all__ = [
    "DatasetManager",
    "TrainingManager",
    "__version__",
]
