"""The PyPI visibility gate polls the resolver surface (issue #3815).

The defect these cover: the gate used to poll the JSON metadata API while
the step it guards resolves against the simple index. The two are separate
caches, so the gate could pass on a version ``pip`` could not yet install.

The central case is ``test_version_absent_from_index_is_not_visible`` —
a version the project page knows about but offers no distribution for is
exactly the input the old check passed and the RPM build then failed on.
Each negative is paired with a positive control, so an over-strict gate
that refuses everything cannot satisfy the suite on its own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wait_for_pypi_visibility.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wait_for_pypi_visibility", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _index(*filenames: str) -> str:
    """A PEP 503 simple-index page offering ``filenames``."""
    links = "\n".join(f'<a href="/packages/{name}#sha256=deadbeef">{name}</a><br/>' for name in filenames)
    return f"<!DOCTYPE html><html><head><title>Links for bernstein</title></head><body>{links}</body></html>"


class TestIndexHasVersion:
    """The resolvability predicate itself."""

    def test_wheel_for_the_exact_version_is_visible(self) -> None:
        body = _index("bernstein-3.15.1-py3-none-any.whl")
        assert gate.index_has_version(body, "bernstein", "3.15.1") is True

    def test_sdist_for_the_exact_version_is_visible(self) -> None:
        body = _index("bernstein-3.15.1.tar.gz")
        assert gate.index_has_version(body, "bernstein", "3.15.1") is True

    def test_version_absent_from_index_is_not_visible(self) -> None:
        """The v3.15.1 failure: neighbouring versions present, this one absent.

        The JSON API knew about 3.15.1 while the simple index still served
        only 3.15.0, and the old gate passed on precisely this state.
        """
        body = _index("bernstein-3.14.159-py3-none-any.whl", "bernstein-3.15.0-py3-none-any.whl")
        assert gate.index_has_version(body, "bernstein", "3.15.1") is False

    def test_a_mention_without_a_distribution_is_not_visible(self) -> None:
        """ "Resolvable" means a file exists, not that the page says the number."""
        body = "<html><body>Links for bernstein — latest is 3.15.1</body></html>"
        assert gate.index_has_version(body, "bernstein", "3.15.1") is False

    def test_a_longer_version_does_not_satisfy_a_prefix(self) -> None:
        """3.15.10 must not be read as proof that 3.15.1 is there."""
        body = _index("bernstein-3.15.10-py3-none-any.whl")
        assert gate.index_has_version(body, "bernstein", "3.15.1") is False


class TestWaitForVisibility:
    """The polling loop and the exit codes the workflow reads."""

    def test_returns_zero_once_the_version_is_resolvable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_fetch", lambda url, timeout: _index("bernstein-3.15.1-py3-none-any.whl"))

        exit_code = gate.wait_for_visibility("3.15.1", attempts=3, delay=0, sleep=lambda _: None, stream=sys.stdout)

        assert exit_code == 0

    def test_returns_nonzero_when_the_index_never_offers_the_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The failing-first assertion from #3815: stub index omits the version."""
        monkeypatch.setattr(gate, "_fetch", lambda url, timeout: _index("bernstein-3.15.0-py3-none-any.whl"))

        exit_code = gate.wait_for_visibility("3.15.1", attempts=3, delay=0, sleep=lambda _: None, stream=sys.stdout)

        assert exit_code == 1

    def test_timeout_message_names_a_stalled_index_not_a_broken_build(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(gate, "_fetch", lambda url, timeout: _index("bernstein-3.15.0-py3-none-any.whl"))

        gate.wait_for_visibility("3.15.1", attempts=2, delay=0, sleep=lambda _: None, stream=sys.stdout)

        message = capsys.readouterr().out
        assert "not a packaging defect" in message
        assert "index propagation" in message

    def test_unreadable_index_is_reported_as_a_fault_not_a_missing_release(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An index that never answers is a different diagnosis from one that lags."""
        monkeypatch.setattr(gate, "_fetch", lambda url, timeout: None)

        exit_code = gate.wait_for_visibility("3.15.1", attempts=2, delay=0, sleep=lambda _: None, stream=sys.stdout)

        assert exit_code == 1
        assert "index or network fault" in capsys.readouterr().out

    def test_polls_again_after_a_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A release that lands mid-wait is picked up rather than failed."""
        bodies = [
            _index("bernstein-3.15.0-py3-none-any.whl"),
            _index("bernstein-3.15.0-py3-none-any.whl"),
            _index("bernstein-3.15.1-py3-none-any.whl"),
        ]
        monkeypatch.setattr(gate, "_fetch", lambda url, timeout: bodies.pop(0))
        slept: list[float] = []

        exit_code = gate.wait_for_visibility("3.15.1", attempts=5, delay=10, sleep=slept.append, stream=sys.stdout)

        assert exit_code == 0
        assert slept == [10, 10], "should sleep between polls, and stop as soon as it resolves"
