"""Orchestration package.

Defines the framework's orchestrator abstraction. The core framework
depends on this interface only; Airflow or any other infrastructure
backend is plugged in via an adapter that implements ``Orchestrator``.

Dependency direction:

    Framework
        -> Orchestrator (this package)
        -> LocalDockerOrchestrator | AirflowOrchestrator | ...
"""
