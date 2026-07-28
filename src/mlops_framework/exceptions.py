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


# --- Orchestration ----------------------------------------------------- #


class OrchestrationError(MLopsFrameworkError):
    """Base exception for orchestrator-related errors."""

    pass


class ExecutionNotFoundError(OrchestrationError):
    """Raised when an orchestrator has no record of the given execution ID."""

    pass


class OrchestratorConfigError(OrchestrationError):
    """Raised when the orchestrator is mis-configured or cannot run."""

    pass


# --- Experiment tracking ----------------------------------------------- #


class ExperimentTrackingError(MLopsFrameworkError):
    """Base exception for experiment tracker errors."""

    pass


# --- Model lifecycle --------------------------------------------------- #


class ModelError(MLopsFrameworkError):
    """Base exception for model lifecycle errors."""

    pass


class ModelNotFoundError(ModelError):
    """Raised when a Model is not found."""

    pass


class ModelVersionNotFoundError(ModelError):
    """Raised when a ModelVersion is not found."""

    pass


class DuplicateModelNameError(ModelError):
    """Raised when attempting to create a Model with a duplicate name."""

    pass


class InvalidModelStateTransitionError(ModelError):
    """Raised when an invalid model lifecycle transition is attempted."""

    pass


# --- Readiness / Eligibility / Drift / Promotion ---------------------- #


class ReadinessError(MLopsFrameworkError):
    """Base exception for dataset-readiness errors."""

    pass


class EligibilityError(MLopsFrameworkError):
    """Base exception for training-eligibility errors."""

    pass


class NotEligibleError(EligibilityError):
    """Raised when a training-eligibility policy rejects a retraining request.

    Carries an explainable list of reasons for the rejection.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__(
            "Training not eligible: " + "; ".join(self.reasons)
        )


class DriftError(MLopsFrameworkError):
    """Base exception for drift-detection errors."""

    pass


class PromotionPolicyError(MLopsFrameworkError):
    """Base exception for model-promotion policy errors."""

    pass


class ModelNotApprovedError(PromotionPolicyError):
    """Raised when a promotion policy rejects a model.

    Carries an explainable list of reasons.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__(
            "Model not approved for promotion: " + "; ".join(self.reasons)
        )


# --- Events / Serving ------------------------------------------------ #


class EventPublisherError(MLopsFrameworkError):
    """Base exception for event publishing errors."""

    pass


class ServingError(MLopsFrameworkError):
    """Base exception for serving-bridge errors."""

    pass
