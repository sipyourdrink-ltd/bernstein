"""Tests for the pluggy-style plugin source."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from bernstein.core.skills.sources import (
    PLUGIN_ENTRY_POINT_GROUP,
    LocalDirSkillSource,
    PluginSkillSource,
    load_plugin_sources,
)


@dataclass
class _FakeEntryPoint:
    """Tiny stand-in for importlib.metadata.EntryPoint used in tests."""

    name: str
    target: object

    def load(self) -> object:
        return self.target


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[_FakeEntryPoint]) -> None:
    def fake_entry_points(
        *,
        group: str | None = None,
    ) -> Iterator[_FakeEntryPoint] | list[_FakeEntryPoint]:
        assert group == PLUGIN_ENTRY_POINT_GROUP
        return list(eps)

    monkeypatch.setattr(
        "bernstein.core.skills.sources.plugin.entry_points",
        fake_entry_points,
    )


def test_load_plugin_sources_returns_wrapped_instance(
    sample_skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = LocalDirSkillSource(sample_skills_root, source_name="custom-inner")

    def factory() -> LocalDirSkillSource:
        return inner

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("my-pack", factory)])

    sources = load_plugin_sources()

    assert len(sources) == 1
    wrapped = sources[0]
    assert isinstance(wrapped, PluginSkillSource)
    assert wrapped.name == "plugin:my-pack"
    assert wrapped.inner is inner
    # Wrapped source iterates through the inner source.
    artifacts = wrapped.iter_skills()
    assert {a.manifest.name for a in artifacts} == {"alpha", "beta", "gamma"}


def test_load_plugin_sources_accepts_instance_directly(
    sample_skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = LocalDirSkillSource(sample_skills_root, source_name="direct-instance")
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("direct", inner)])

    sources = load_plugin_sources()
    assert len(sources) == 1
    assert sources[0].name == "plugin:direct"


def test_load_plugin_sources_skips_broken_factory(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> object:
        raise RuntimeError("intentional test failure")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("broken", boom)])

    with caplog.at_level("WARNING"):
        sources = load_plugin_sources()

    assert sources == []
    assert "broken" in caplog.text


def test_load_plugin_sources_skips_non_source_return(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def returns_str() -> str:
        return "not a source"

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("bad-return", returns_str)])

    with caplog.at_level("WARNING"):
        sources = load_plugin_sources()
    assert sources == []
    assert "bad-return" in caplog.text


def test_load_plugin_sources_skips_non_callable_non_source(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("weird", 42)])

    with caplog.at_level("WARNING"):
        sources = load_plugin_sources()
    assert sources == []
    assert "weird" in caplog.text
