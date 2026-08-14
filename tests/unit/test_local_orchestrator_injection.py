"""Regression tests: ``pipeline_id`` must never be executable as code.

``LocalDockerOrchestrator`` used to build the child process's program by
interpolating the caller's ``pipeline_id`` into source text::

    f"from {module} import {fn} as _entry\\n"

``pipeline_id`` arrives verbatim from ``POST /api/schedules`` and from
any SDK caller, so a value containing a newline that kept the surrounding
lines syntactically valid executed arbitrary code inside the app
container. The payload in :data:`INJECTION_PAYLOAD` is the real one — it
wrote a file and returned, with the execution merely reported FAILED
afterwards.

Two independent defences are pinned here, either of which alone closes
the hole (see the module docstring in ``orchestration/local.py``):

1. :func:`_resolve_entry_point` rejects anything that is not a dotted
   import path plus an identifier;
2. the child program is a constant, and the entry point travels as
   ``argv`` — so even a validator that let something through could not
   turn it into code.
"""

from __future__ import annotations

import time

import pytest

from mlops_framework.exceptions import OrchestratorConfigError
from mlops_framework.orchestration.local import (
    _BOOTSTRAP,
    LocalDockerOrchestrator,
    _resolve_entry_point,
)

# The exact shape that worked: `module` closes the generated `from …`
# statement, runs a payload line, then re-opens a valid `from json`
# so the trailing ` import loads as _entry` still parses.
INJECTION_PAYLOAD = (
    "json import loads\n"
    "open({path!r}, 'w').write('pwned')\n"
    "from json:loads"
)

REJECTED = [
    pytest.param("json import loads\nprint(1)\nfrom json:loads", id="newline-payload"),
    pytest.param("os;import subprocess:system", id="semicolon"),
    pytest.param("a b:main", id="space-in-module"),
    pytest.param("mod:fn()", id="call-syntax-in-callable"),
    pytest.param("mod:fn;x", id="semicolon-in-callable"),
    pytest.param("../etc/passwd:main", id="path-traversal"),
    pytest.param("mod:", id="empty-callable"),
    pytest.param(":fn", id="empty-module"),
    pytest.param("", id="empty"),
    pytest.param("mod\n:fn", id="newline-before-colon"),
    pytest.param("1mod:fn", id="module-starts-with-digit"),
]

ACCEPTED = [
    ("tests._pipelines.pipelines:success", ("tests._pipelines.pipelines", "success")),
    ("package.module:callable_name", ("package.module", "callable_name")),
    ("module", ("module", "main")),
    ("_private.Mod9:_fn2", ("_private.Mod9", "_fn2")),
]


class TestEntryPointValidation:
    @pytest.mark.parametrize("pipeline_id", REJECTED)
    def test_rejects_anything_that_is_not_an_import_path(self, pipeline_id):
        with pytest.raises(OrchestratorConfigError):
            _resolve_entry_point(pipeline_id)

    @pytest.mark.parametrize("pipeline_id,expected", ACCEPTED)
    def test_still_accepts_legitimate_entry_points(self, pipeline_id, expected):
        assert _resolve_entry_point(pipeline_id) == expected


class TestChildProgramIsConstant:
    """The second defence: nothing caller-supplied is ever parsed as
    Python, so the first one failing open would still not be RCE."""

    def test_bootstrap_has_no_interpolation_points(self):
        assert "{" not in _BOOTSTRAP.replace("json.loads(sys.stdin.read() or '{}')", "")
        assert "import importlib" in _BOOTSTRAP
        assert "sys.argv[1]" in _BOOTSTRAP and "sys.argv[2]" in _BOOTSTRAP

    def test_entry_point_is_passed_as_argv_not_source(self, monkeypatch):
        captured = {}

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = argv
                self.stdin = self.stdout = self.stderr = None

            def poll(self):
                return 0

        monkeypatch.setattr(
            "mlops_framework.orchestration.local.subprocess.Popen", _FakePopen
        )
        orch = LocalDockerOrchestrator()
        orch.trigger_pipeline("tests._pipelines.pipelines:success", {})

        argv = captured["argv"]
        # [python, "-c", <constant program>, module, callable]
        assert argv[2] == _BOOTSTRAP
        assert argv[3:] == ["tests._pipelines.pipelines", "success"]


class TestInjectionDoesNotExecute:
    def test_payload_is_refused_and_writes_no_file(self, tmp_path):
        marker = tmp_path / "pwned.txt"
        pipeline_id = INJECTION_PAYLOAD.format(path=str(marker))

        orch = LocalDockerOrchestrator()
        try:
            with pytest.raises(OrchestratorConfigError):
                orch.trigger_pipeline(pipeline_id, {})
            # Nothing was spawned, so nothing could have run — but give a
            # would-be child the chance to land the file before asserting.
            time.sleep(0.5)
            assert not marker.exists(), "injected code executed"
        finally:
            orch.shutdown()

    def test_the_same_payload_through_a_schedule_is_refused(self, tmp_path):
        """The reachable path: pipeline_id comes from POST /api/schedules
        and is handed to the orchestrator by ``run_schedule_now``."""
        marker = tmp_path / "pwned-via-schedule.txt"
        pipeline_id = INJECTION_PAYLOAD.format(path=str(marker))

        orch = LocalDockerOrchestrator()
        try:
            with pytest.raises(OrchestratorConfigError):
                orch.trigger_pipeline(pipeline_id, {"training_run_id": 1})
            time.sleep(0.5)
            assert not marker.exists()
        finally:
            orch.shutdown()
