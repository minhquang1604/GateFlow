"""Local orchestrator that runs pipelines as local Python subprocesses.

This implementation is intentionally lightweight: it spawns a Python
process and tracks it by an in-memory execution id. It exists so the
framework's business logic can be exercised end-to-end without Airflow
infrastructure.

Pipeline invocation contract
----------------------------
A "pipeline" is a Python entry point, identified as
``"package.module:function"``. The subprocess receives the config as a
JSON blob on stdin, and is expected to write a single JSON line on
stdout to report ``{"status": "SUCCESS"|"FAILED", ...}``. Exit code 0
is interpreted as SUCCESS, anything else as FAILED. ``cancel_execution``
terminates the running process.

Why the entry point is validated *and* passed out-of-band
---------------------------------------------------------
``pipeline_id`` is attacker-reachable: it arrives verbatim from
``POST /api/schedules`` (``schedules.py``'s ``CreateScheduleRequest``)
and from any SDK/API caller creating a training run. It used to be
interpolated into the child's source text
(``f"from {module} import {fn} as _entry"``), which made a newline in
the value arbitrary code execution inside the app container — the
value only had to keep the surrounding two lines syntactically valid.

Both halves of that are now closed, deliberately redundantly:

* :func:`_resolve_entry_point` rejects anything that is not a dotted
  identifier path plus an identifier, so a payload never gets this far;
* :data:`_BOOTSTRAP` is a fixed, constant program. The module and
  callable travel as ``argv`` and are resolved with
  :func:`importlib.import_module` + :func:`getattr`, so nothing a
  caller supplies is ever parsed as Python — even if the validator
  above is one day loosened or bypassed.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from mlops_framework.exceptions import (
    ExecutionNotFoundError,
    OrchestratorConfigError,
)
from mlops_framework.orchestration.base import (
    ExecutionState,
    ExecutionStatus,
    Orchestrator,
)


def _now() -> datetime:
    return datetime.now(UTC)


# A dotted import path and a plain callable name — exactly what
# `import a.b.c` and `getattr(mod, "name")` accept, and nothing else.
# Anchored, so a value that merely *starts* like an identifier (the
# shape every injection payload has) is rejected rather than truncated.
#
# \Z, not $: in Python `$` also matches just before a trailing newline,
# so `^\w+$` accepts "mod\n" — the one character these patterns exist to
# reject. tests/unit/test_local_orchestrator_injection.py pins this.
_MODULE_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_CALLABLE_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _resolve_entry_point(pipeline_id: str) -> tuple[str, str]:
    """Parse ``"module:callable"`` into ``(module, callable)``.

    Raises:
        OrchestratorConfigError: if either half is not a plain Python
            identifier path. See the module docstring — this value is
            reachable from unauthenticated-by-default HTTP input, so
            "looks roughly right" is not a strong enough check.
    """
    if ":" in pipeline_id:
        module, _, fn = pipeline_id.partition(":")
    else:
        module, fn = pipeline_id, "main"
    if not _MODULE_RE.match(module) or not _CALLABLE_RE.match(fn):
        raise OrchestratorConfigError(
            f"Invalid pipeline_id {pipeline_id!r}; expected 'module:callable' "
            "where 'module' is a dotted import path and 'callable' is a "
            "Python identifier"
        )
    return module, fn


# Constant by construction: the entry point travels as argv (see
# trigger_pipeline), never as text spliced into this program.
_BOOTSTRAP = (
    "import importlib, json, sys\n"
    "_entry = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])\n"
    "cfg = json.loads(sys.stdin.read() or '{}')\n"
    "result = _entry(cfg)\n"
    "if result is not None:\n"
    "    sys.stdout.write(json.dumps(result) + '\\n')\n"
    "sys.stdout.flush()\n"
    "sys.exit(0)\n"
)


class _LocalExecution:
    """Internal bookkeeping for a single subprocess execution."""

    def __init__(
        self,
        execution_id: str,
        pipeline_id: str,
        process: subprocess.Popen,
        state: ExecutionState,
        started_at: datetime,
    ) -> None:
        self.execution_id = execution_id
        self.pipeline_id = pipeline_id
        self.process = process
        self.state = state
        self.started_at = started_at
        self.finished_at: datetime | None = None
        self.exit_code: int | None = None
        self.message: str | None = None
        self.metadata: dict[str, Any] = {}
        self._stdout_parts: list[str] = []
        self._stderr_parts: list[str] = []
        self._reader_thread: threading.Thread | None = None

    def refresh_if_finished(self) -> None:
        if self.state in {ExecutionState.SUCCESS, ExecutionState.FAILED, ExecutionState.CANCELLED}:
            return
        proc = self.process
        if proc is None:
            return
        rc = proc.poll()
        if rc is None:
            return
        # Process has exited — give the reader thread a moment to drain.
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
        self.exit_code = rc
        self.finished_at = _now()
        stdout = "".join(self._stdout_parts)
        stderr = "".join(self._stderr_parts)
        if rc == 0:
            self.state = ExecutionState.SUCCESS
            if stdout:
                try:
                    payload = json.loads(stdout.strip().splitlines()[-1])
                    if isinstance(payload, dict):
                        self.metadata.update(payload)
                except (ValueError, IndexError):
                    pass
        else:
            self.state = ExecutionState.FAILED
            self.message = (stderr or stdout or "").strip() or f"Exit code {rc}"


class LocalDockerOrchestrator(Orchestrator):
    """Local subprocess orchestrator.

    Suitable for fast local development and integration tests. The name
    keeps "Docker" for forward compatibility with a real Docker-based
    implementation, but the current implementation uses ``subprocess``
    so it has no external dependencies.
    """

    def __init__(self, python_executable: str | None = None) -> None:
        self._python = python_executable or sys.executable
        self._executions: dict[str, _LocalExecution] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Orchestrator API
    # ------------------------------------------------------------------ #

    def trigger_pipeline(
        self,
        pipeline_id: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        module, fn = _resolve_entry_point(pipeline_id)
        execution_id = uuid.uuid4().hex
        config = config or {}

        try:
            proc = subprocess.Popen(
                [self._python, "-c", _BOOTSTRAP, module, fn],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise OrchestratorConfigError(
                f"Failed to start local pipeline {pipeline_id!r}: {exc}"
            ) from exc

        record = _LocalExecution(
            execution_id=execution_id,
            pipeline_id=pipeline_id,
            process=proc,
            state=ExecutionState.RUNNING,
            started_at=_now(),
        )
        with self._lock:
            self._executions[execution_id] = record

        # Send config in a background thread so we never block on a full
        # pipe. The thread also drains stdout/stderr.
        thread = threading.Thread(
            target=self._drive_subprocess,
            args=(record, config),
            daemon=True,
        )
        record._reader_thread = thread
        thread.start()
        return execution_id

    def get_execution_status(self, execution_id: str) -> ExecutionStatus:
        record = self._get(execution_id)
        record.refresh_if_finished()
        with self._lock:
            return self._to_status(record)

    def cancel_execution(self, execution_id: str) -> ExecutionStatus:
        record = self._get(execution_id)
        with self._lock:
            if record.state in {ExecutionState.SUCCESS, ExecutionState.FAILED}:
                return self._to_status(record)
            proc = record.process
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except ProcessLookupError:
                    pass
            record.state = ExecutionState.CANCELLED
            record.finished_at = _now()
            return self._to_status(record)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _drive_subprocess(
        self,
        record: _LocalExecution,
        config: dict[str, Any],
    ) -> None:
        proc = record.process
        try:
            assert proc.stdin is not None
            try:
                proc.stdin.write(json.dumps(config))
                proc.stdin.flush()
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

            assert proc.stdout is not None and proc.stderr is not None
            for line in proc.stdout:
                record._stdout_parts.append(line)
            for line in proc.stderr:
                record._stderr_parts.append(line)
        except Exception as exc:  # pragma: no cover - defensive
            record.message = f"driver thread error: {exc}"

    def _get(self, execution_id: str) -> _LocalExecution:
        with self._lock:
            record = self._executions.get(execution_id)
        if record is None:
            raise ExecutionNotFoundError(
                f"Unknown execution id {execution_id!r}"
            )
        return record

    @staticmethod
    def _to_status(record: _LocalExecution) -> ExecutionStatus:
        return ExecutionStatus(
            execution_id=record.execution_id,
            state=record.state,
            pipeline_id=record.pipeline_id,
            started_at=record.started_at,
            finished_at=record.finished_at,
            exit_code=record.exit_code,
            message=record.message,
            metadata=record.metadata,
        )

    def shutdown(self) -> None:
        """Terminate any still-running executions. Intended for tests."""
        with self._lock:
            records = list(self._executions.values())
        for record in records:
            proc = record.process
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
