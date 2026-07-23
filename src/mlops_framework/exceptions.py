"""MLOps Framework exceptions."""


class MLopsFrameworkError(Exception):
    """Base exception for MLOps Framework."""

    pass


class DatasetError(MLopsFrameworkError):
    """Exception raised for dataset-related errors."""

    pass


class DatasetNotFoundError(DatasetError):
    """Exception raised when a dataset is not found."""

    pass


class DuplicateDatasetNameError(DatasetError):
    """Exception raised when attempting to create a dataset with a duplicate name."""

    pass


class DatasetVersionError(MLopsFrameworkError):
    """Exception raised for dataset version-related errors."""

    pass


class DatasetVersionNotFoundError(DatasetVersionError):
    """Exception raised when a dataset version is not found."""

    pass


class ImmutableDatasetVersionError(DatasetVersionError):
    """Exception raised when attempting to modify an immutable dataset version."""

    pass


class InvalidVersionNumberError(DatasetVersionError):
    """Exception raised when an invalid version number is provided."""

    pass


class TrainingRunError(MLopsFrameworkError):
    """Exception raised for training run-related errors."""

    pass


class TrainingRunNotFoundError(TrainingRunError):
    """Exception raised when a training run is not found."""

    pass


class InvalidStatusTransitionError(TrainingRunError):
    """Exception raised when an invalid status transition is attempted."""

    pass


class ChecksumError(MLopsFrameworkError):
    """Exception raised for checksum-related errors."""

    pass


class SchemaHashError(MLopsFrameworkError):
    """Exception raised for schema hash-related errors."""

    pass
