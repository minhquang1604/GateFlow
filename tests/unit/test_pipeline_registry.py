"""Unit tests for the PipelineRegistry."""

from __future__ import annotations

import pytest

from mlops_framework.pipeline import (
    PipelineEntry,
    PipelineNotFoundError,
    PipelineRegistry,
)


class TestRegistration:
    def test_register_and_resolve(self):
        r = PipelineRegistry()
        r.register(
            PipelineEntry(
                name="xgboost",
                pipeline_id="pkg.train:main",
                parameters={"max_depth": 6},
            )
        )
        assert r.resolve("xgboost") == "pkg.train:main"

    def test_register_overwrites(self):
        r = PipelineRegistry()
        r.register(PipelineEntry(name="a", pipeline_id="v1"))
        r.register(PipelineEntry(name="a", pipeline_id="v2"))
        assert r.resolve("a") == "v2"
        assert len(r) == 1

    def test_register_many(self):
        r = PipelineRegistry()
        r.register_many(
            [
                PipelineEntry(name="a", pipeline_id="pa"),
                PipelineEntry(name="b", pipeline_id="pb"),
            ]
        )
        assert sorted(r.names()) == ["a", "b"]
        assert len(r) == 2

    def test_init_with_entries(self):
        r = PipelineRegistry(
            [
                PipelineEntry(name="a", pipeline_id="pa"),
                PipelineEntry(name="b", pipeline_id="pb"),
            ]
        )
        assert r.resolve("a") == "pa"
        assert r.resolve("b") == "pb"


class TestLookup:
    def test_get_returns_full_entry(self):
        r = PipelineRegistry()
        entry = PipelineEntry(
            name="x",
            pipeline_id="pid",
            description="d",
            parameters={"k": "v"},
        )
        r.register(entry)
        got = r.get("x")
        assert got.name == "x"
        assert got.pipeline_id == "pid"
        assert got.description == "d"
        assert got.parameters == {"k": "v"}

    def test_contains(self):
        r = PipelineRegistry()
        r.register(PipelineEntry(name="x", pipeline_id="pid"))
        assert "x" in r
        assert "y" not in r

    def test_names_sorted(self):
        r = PipelineRegistry()
        for n in ["c", "a", "b"]:
            r.register(PipelineEntry(name=n, pipeline_id=n))
        assert r.names() == ["a", "b", "c"]


class TestErrors:
    def test_resolve_missing_raises(self):
        r = PipelineRegistry()
        with pytest.raises(PipelineNotFoundError) as exc:
            r.resolve("nope")
        assert "nope" in str(exc.value)

    def test_get_missing_raises(self):
        r = PipelineRegistry()
        with pytest.raises(PipelineNotFoundError):
            r.get("missing")

    def test_pipeline_not_found_is_keyerror(self):
        r = PipelineRegistry()
        with pytest.raises(KeyError):
            r.resolve("missing")


class TestEntryDefaults:
    def test_default_parameters_empty(self):
        e = PipelineEntry(name="x", pipeline_id="pid")
        assert e.parameters == {}
        assert e.description == ""

    def test_default_description_empty(self):
        e = PipelineEntry(name="x", pipeline_id="pid", parameters={"a": 1})
        assert e.description == ""
