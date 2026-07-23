"""Test pipeline entry points used by LocalDockerOrchestrator tests.

These are imported by name by the orchestrator tests via
``pipeline_id="tests._pipelines.success:main"`` etc.
"""

import sys


def success(config: dict) -> dict:
    """Succeeds, returns a small payload."""
    return {"status": "SUCCESS", "config_keys": sorted((config or {}).keys())}


def fail(config: dict) -> None:
    """Fails with a non-zero exit code."""
    sys.stderr.write("intentional failure for test\n")
    sys.exit(2)


def slow(config: dict) -> dict:
    """Sleeps long enough to be cancellable in tests."""
    import time
    time.sleep(2.0)
    return {"status": "SUCCESS"}


def raises(config: dict) -> None:
    raise RuntimeError("pipeline exploded")
