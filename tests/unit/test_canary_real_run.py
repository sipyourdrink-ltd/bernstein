"""Unit tests for the nightly real-run canary's flow-selection and failure-emission.

The canary script (``scripts/canary_real_run.py``) drives a handful of
genuinely-runtime orchestration paths against the deterministic stub
adapter so a scheduled CI job catches integration-level breaks that the
mocked unit suite cannot see. These tests pin the two behaviours the
workflow depends on, *without* any network or real telemetry DSN:

* the success path runs every registered flow and returns exit code 0;
* an injected flow failure returns a non-zero exit code, emits exactly
  one telemetry event carrying enough context for the GlitchTip-to-eval
  ingester to synthesise a regression case (flow name, exception type,
  top frame, ``environment=canary``), and leaves no worktree / temp-dir
  behind.

The canary itself is deliberately *not* collected by the main pytest
run (it is a scheduled job); these unit tests exercise its seams with
stub flows so PR CI stays fast.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "canary_real_run.py"


def _load_canary() -> ModuleType:
    """Import ``scripts/canary_real_run.py`` as a module by file path.

    The script lives outside the importable package, so it is loaded via
    an explicit spec rather than a normal import. The module is cached in
    ``sys.modules`` under a private name so repeated loads are cheap.
    """
    mod_name = "_canary_real_run_under_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def canary() -> ModuleType:
    return _load_canary()


class _RecordingEmitter:
    """Capture telemetry emissions instead of touching any DSN."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        exc: BaseException,
        *,
        flow: str,
        tags: dict[str, str],
        extra: dict[str, Any],
    ) -> None:
        self.calls.append(
            {
                "exc": exc,
                "flow": flow,
                "tags": dict(tags),
                "extra": dict(extra),
            }
        )


# ---------------------------------------------------------------------------
# Flow registry
# ---------------------------------------------------------------------------


def test_default_flow_registry_is_small_and_named(canary: ModuleType) -> None:
    """The shipped registry stays focused (1-3 representative flows)."""
    flows = canary.default_flows()
    assert 1 <= len(flows) <= 3
    # Each entry is a name -> zero-arg callable.
    for name, fn in flows.items():
        assert isinstance(name, str) and name
        assert callable(fn)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_run_canary_success_returns_zero_and_emits_nothing(canary: ModuleType) -> None:
    """All flows passing -> exit 0, no telemetry events."""
    emitter = _RecordingEmitter()
    ran: list[str] = []

    def _ok_a() -> None:
        ran.append("a")

    def _ok_b() -> None:
        ran.append("b")

    rc = canary.run_canary(
        flows={"alpha": _ok_a, "beta": _ok_b},
        emit=emitter,
    )

    assert rc == 0
    assert sorted(ran) == ["a", "b"]
    assert emitter.calls == []


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_run_canary_failure_returns_nonzero_and_emits_one_event(canary: ModuleType) -> None:
    """An injected failure -> non-zero exit and exactly one shaped event."""
    emitter = _RecordingEmitter()

    def _boom() -> None:
        raise RuntimeError("synthetic canary break")

    rc = canary.run_canary(flows={"explode": _boom}, emit=emitter)

    assert rc != 0
    assert len(emitter.calls) == 1
    call = emitter.calls[0]
    assert call["flow"] == "explode"
    assert isinstance(call["exc"], RuntimeError)
    # The ingester reads these to synthesise a case: environment, flow,
    # exception type, and a top frame must all be present.
    tags = call["tags"]
    assert tags["environment"] == "canary"
    assert tags["flow"] == "explode"
    assert tags["exc_type"] == "RuntimeError"
    assert tags["top_frame"]  # non-empty file:line of the failing frame
    extra = call["extra"]
    assert "synthetic canary break" in str(extra.get("exc_value", ""))


def test_run_canary_runs_all_flows_even_after_a_failure(canary: ModuleType) -> None:
    """One failing flow does not stop the others; every break is reported."""
    emitter = _RecordingEmitter()
    ran: list[str] = []

    def _ok() -> None:
        ran.append("ok")

    def _boom() -> None:
        raise ValueError("second-flow break")

    rc = canary.run_canary(
        flows={"good": _ok, "bad": _boom},
        emit=emitter,
    )

    assert rc != 0
    assert "ok" in ran  # the healthy flow still ran
    assert len(emitter.calls) == 1
    assert emitter.calls[0]["flow"] == "bad"
    assert emitter.calls[0]["tags"]["exc_type"] == "ValueError"


def test_default_emit_is_resolved_when_not_injected(canary: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``emit`` argument the canary routes through error_capture.

    The default emitter must be the existing observability client, never a
    bespoke emitter. We assert it forwards a real exception and the
    ``environment=canary`` tag through ``error_capture.capture_exception``.
    """
    from bernstein.core.observability import error_capture

    calls: list[dict[str, Any]] = []

    def _fake_capture(
        exc: BaseException,
        *,
        category: str,
        tags: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        calls.append({"exc": exc, "category": category, "tags": dict(tags or {}), "extra": dict(extra or {})})

    monkeypatch.setattr(error_capture, "capture_exception", _fake_capture)

    def _boom() -> None:
        raise RuntimeError("default-path break")

    rc = canary.run_canary(flows={"explode": _boom})

    assert rc != 0
    assert len(calls) == 1
    assert calls[0]["category"] == "canary"
    assert calls[0]["tags"]["environment"] == "canary"
    assert calls[0]["tags"]["flow"] == "explode"
    assert isinstance(calls[0]["exc"], RuntimeError)


# ---------------------------------------------------------------------------
# No-leak guarantee for the real flows
# ---------------------------------------------------------------------------


def _count_worktrees(repo_root: Path) -> int:
    import subprocess

    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    # Each worktree entry begins with a "worktree <path>" line.
    return sum(1 for line in out.splitlines() if line.startswith("worktree "))


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Iterator[Path]:
    import subprocess

    repo = tmp_path / "canary_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "canary@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Canary"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("# canary\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
    yield repo


# The git-worktree and subprocess-spawn canary flows are platform-neutral: the
# spawn flow drives the mock adapter (a plain cross-platform ``Popen`` reaped via
# ``proc.wait()``) and the worktree flow drives ``git worktree add/remove``, both
# of which run on Windows. The class therefore runs on every OS.
class TestRealCanaryFlows:
    def test_worktree_flow_creates_and_tears_down_without_leak(self, canary: ModuleType, temp_git_repo: Path) -> None:
        """The real git-worktree flow leaves the repo with no extra worktrees."""
        before = _count_worktrees(temp_git_repo)
        canary.flow_git_worktree(repo_root=temp_git_repo)
        after = _count_worktrees(temp_git_repo)
        assert after == before, "git-worktree flow leaked a worktree"

    def test_subprocess_spawn_flow_runs_real_process(self, canary: ModuleType, tmp_path: Path) -> None:
        """The spawn flow drives a real subprocess and observes a clean exit."""
        # Should not raise; returns the reaped exit code (0 for the stub adapter).
        code = canary.flow_subprocess_spawn(workdir=tmp_path)
        assert code == 0


def test_audit_and_lineage_flow_verifies_chain(canary: ModuleType, tmp_path: Path) -> None:
    """The audit + lineage flow appends, then verifies, real signed records."""
    # Should not raise; a tampered or unverifiable chain would surface as
    # an exception that the runner converts into a telemetry event.
    canary.flow_audit_and_lineage(state_dir=tmp_path)
