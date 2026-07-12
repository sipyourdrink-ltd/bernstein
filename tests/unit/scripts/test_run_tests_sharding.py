"""Unit tests for the deterministic sharding partition in ``scripts/run_tests.py``.

The CI Test job fans a single ~1.4k-file unit suite out across N parallel
runners. Each runner invokes ``run_tests.py --shard i/N`` and must execute a
disjoint slice of the discovered file list. The partition has to be:

- **Complete**: the union of all shards equals the full file list (no file is
  silently dropped, which would mask a regression).
- **Disjoint**: no file runs on two shards (wasted runner minutes + double
  reporting).
- **Deterministic + stable**: the same ``(files, i, N)`` always yields the same
  slice across runs and across machines, so a failing shard reruns identically.
  The repo's whole identity is determinism, so this is load-bearing.
- **Balanced**: shard sizes differ by at most one, so no single runner becomes
  the long pole.

These tests pin those four properties plus the ``i/N`` spec parser.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_tests.py"


@pytest.fixture
def run_tests_module() -> Generator[ModuleType, None, None]:
    """Load scripts/run_tests.py as an importable module."""
    spec = importlib.util.spec_from_file_location(
        "run_tests_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _fixed_files(count: int) -> list[Path]:
    """A deterministic, sorted file list standing in for discovered tests."""
    return sorted(Path(f"tests/unit/test_file_{i:04d}.py") for i in range(count))


# --- parse_shard_spec ------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1/4", (1, 4)),
        ("4/4", (4, 4)),
        ("2/10", (2, 10)),
        ("1/1", (1, 1)),
    ],
)
def test_parse_shard_spec_valid(run_tests_module: ModuleType, spec: str, expected: tuple[int, int]) -> None:
    assert run_tests_module.parse_shard_spec(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "0/4",  # index below 1
        "5/4",  # index above count
        "1/0",  # zero count
        "-1/4",  # negative index
        "abc",  # not a fraction
        "1",  # missing count
        "1/4/2",  # too many parts
        "1.5/4",  # non-integer
        "",  # empty
    ],
)
def test_parse_shard_spec_invalid_raises(run_tests_module: ModuleType, spec: str) -> None:
    with pytest.raises(ValueError):
        run_tests_module.parse_shard_spec(spec)


# --- shard_files partition properties --------------------------------------


def test_shard_files_covers_every_file_exactly_once(run_tests_module: ModuleType) -> None:
    """Union of all 4 shards == full list; intersection of any pair is empty."""
    files = _fixed_files(1428)
    shard_count = 4

    shards = [run_tests_module.shard_files(files, i, shard_count) for i in range(1, shard_count + 1)]

    union: list[Path] = []
    for shard in shards:
        union.extend(shard)

    # Complete: every file appears.
    assert sorted(union) == sorted(files)
    # Disjoint: no duplicates across shards.
    assert len(union) == len(files)
    assert len(set(union)) == len(files)


def test_shard_files_partitions_are_disjoint_pairwise(run_tests_module: ModuleType) -> None:
    files = _fixed_files(100)
    shard_count = 4
    shards = [set(run_tests_module.shard_files(files, i, shard_count)) for i in range(1, shard_count + 1)]
    for a in range(len(shards)):
        for b in range(a + 1, len(shards)):
            assert shards[a].isdisjoint(shards[b]), f"shards {a + 1} and {b + 1} overlap"


def test_shard_files_is_deterministic_across_runs(run_tests_module: ModuleType) -> None:
    """Same inputs -> identical slice, every time (no hashing salt drift)."""
    files = _fixed_files(257)
    first = run_tests_module.shard_files(files, 2, 4)
    second = run_tests_module.shard_files(files, 2, 4)
    third = run_tests_module.shard_files(list(files), 2, 4)
    assert first == second == third


def test_shard_files_is_balanced(run_tests_module: ModuleType) -> None:
    """Shard sizes differ by at most one (no long-pole runner)."""
    files = _fixed_files(1428)
    sizes = [len(run_tests_module.shard_files(files, i, 4)) for i in range(1, 5)]
    assert max(sizes) - min(sizes) <= 1
    assert sum(sizes) == 1428


def test_shard_files_single_shard_returns_all(run_tests_module: ModuleType) -> None:
    """N=1 is a no-op partition: shard 1/1 == the full list, order preserved."""
    files = _fixed_files(37)
    assert run_tests_module.shard_files(files, 1, 1) == files


def test_shard_files_preserves_relative_order(run_tests_module: ModuleType) -> None:
    """Within a shard, files keep their original discovery order."""
    files = _fixed_files(50)
    shard = run_tests_module.shard_files(files, 1, 4)
    # The shard is a subsequence of the original list.
    indices = [files.index(f) for f in shard]
    assert indices == sorted(indices)


def test_shard_files_more_shards_than_files(run_tests_module: ModuleType) -> None:
    """When N > len(files), trailing shards are empty but coverage holds."""
    files = _fixed_files(3)
    shards = [run_tests_module.shard_files(files, i, 5) for i in range(1, 6)]
    union: list[Path] = []
    for shard in shards:
        union.extend(shard)
    assert sorted(union) == sorted(files)
    # Exactly three shards are non-empty.
    assert sum(1 for s in shards if s) == 3


def test_shard_files_rejects_out_of_range_index(run_tests_module: ModuleType) -> None:
    files = _fixed_files(10)
    with pytest.raises(ValueError):
        run_tests_module.shard_files(files, 0, 4)
    with pytest.raises(ValueError):
        run_tests_module.shard_files(files, 5, 4)


# --- affected empty-selection fail-closed behavior -------------------------


@pytest.mark.parametrize(
    "changed_file",
    [
        "src/bernstein/core/models.py",
        "tests/unit/test_models.py",
        ".github/workflows/ci.yml",
        "scripts/run_tests.py",
        "scripts/test_impact.py",
    ],
)
def test_changed_files_require_tests_for_code_and_workflow_paths(
    run_tests_module: ModuleType,
    changed_file: str,
) -> None:
    assert run_tests_module.changed_files_require_tests([changed_file])


@pytest.mark.parametrize(
    "changed_file",
    [
        "README.md",
        "docs/operations/release.md",
        "CHANGELOG.md",
    ],
)
def test_changed_files_do_not_require_tests_for_docs_paths(
    run_tests_module: ModuleType,
    changed_file: str,
) -> None:
    assert not run_tests_module.changed_files_require_tests([changed_file])


def test_empty_affected_selection_fails_for_source_changes(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_affected_files(_base: str) -> list[Path]:
        return []

    def changed_source_files(_base: str) -> list[str]:
        return ["src/bernstein/core/models.py"]

    monkeypatch.setattr(run_tests_module, "discover_affected_files", no_affected_files)
    monkeypatch.setattr(run_tests_module, "discover_changed_files", changed_source_files)
    monkeypatch.setattr(sys, "argv", ["run_tests.py", "--affected", "origin/main"])

    with pytest.raises(SystemExit) as exc_info:
        run_tests_module.main()

    assert exc_info.value.code == 1


def test_empty_affected_shard_remains_success_when_other_shards_have_tests(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def one_affected_file(_base: str) -> list[Path]:
        return [Path("tests/unit/test_models.py")]

    monkeypatch.setattr(run_tests_module, "discover_affected_files", one_affected_file)
    monkeypatch.setattr(sys, "argv", ["run_tests.py", "--affected", "origin/main", "--shard", "2/2"])

    with pytest.raises(SystemExit) as exc_info:
        run_tests_module.main()

    assert exc_info.value.code == 0


def test_sequential_timeout_message_matches_subprocess_timeout(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run_file(*_: object, **__: object) -> tuple[Path, int, float, str]:
        raise subprocess.TimeoutExpired(cmd=["pytest"], timeout=300)

    monkeypatch.setattr(run_tests_module, "run_file", fake_run_file)

    result = run_tests_module.run_sequential([Path("tests/unit/test_slow.py")], [], fail_fast=True)

    assert result == 1
    assert "TIMEOUT [1/1] test_slow.py (>300s)" in capsys.readouterr().out


def test_run_file_uses_timeout_env_override(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[int] = []

    def fake_run(
        cmd: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int)
        timeouts.append(timeout)
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setenv("BERNSTEIN_TEST_FILE_TIMEOUT_SECONDS", "600")
    monkeypatch.setattr(run_tests_module.subprocess, "run", fake_run)

    _path, code, _duration, output = run_tests_module.run_file(Path("tests/unit/test_slow.py"), [])

    assert code == 0
    assert output == "ok\n"
    assert timeouts == [600]


def test_discover_changed_files_falls_back_to_two_dot_diff_without_merge_base(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[-1] == "origin/main...HEAD":
            raise subprocess.CalledProcessError(128, cmd, stderr="no merge base")
        return subprocess.CompletedProcess(cmd, 0, "src/bernstein/core/models.py\n", "")

    monkeypatch.setattr(run_tests_module.subprocess, "run", fake_run)

    assert run_tests_module.discover_changed_files("origin/main") == ["src/bernstein/core/models.py"]
    assert calls[-1][-1] == "origin/main..HEAD"


def test_retry_on_thread_exhaustion_reruns_and_can_recover(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread-exhaustion failure re-runs once serially and can pass on retry."""
    calls: list[Path] = []

    def fake_run_file(path: Path, *_: object, **__: object) -> tuple[Path, int, float, str]:
        calls.append(path)
        return path, 0, 0.2, "1 passed"

    monkeypatch.setattr(run_tests_module, "run_file", fake_run_file)

    result = run_tests_module.retry_on_thread_exhaustion(
        Path("tests/unit/test_x.py"),
        [],
        code=1,
        output="E   RuntimeError: can't start new thread",
    )

    assert result == (0, 0.2, "1 passed")
    assert calls == [Path("tests/unit/test_x.py")]


def test_retry_on_thread_exhaustion_skips_unrelated_failures(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal assertion failure is not retried (only thread exhaustion is)."""
    called = False

    def fake_run_file(*_: object, **__: object) -> tuple[Path, int, float, str]:
        nonlocal called
        called = True
        return Path("x"), 0, 0.0, ""

    monkeypatch.setattr(run_tests_module, "run_file", fake_run_file)

    result = run_tests_module.retry_on_thread_exhaustion(
        Path("tests/unit/test_x.py"),
        [],
        code=1,
        output="E   AssertionError: expected 1 got 2",
    )

    assert result is None
    assert called is False


def test_retry_on_thread_exhaustion_skips_passing_files(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing file is never retried even if the marker appears in output."""

    def fake_run_file(*_: object, **__: object) -> tuple[Path, int, float, str]:
        raise AssertionError("run_file must not be called for a passing file")

    monkeypatch.setattr(run_tests_module, "run_file", fake_run_file)

    result = run_tests_module.retry_on_thread_exhaustion(
        Path("tests/unit/test_x.py"),
        [],
        code=0,
        output="RuntimeError: can't start new thread (in a captured, non-fatal context)",
    )

    assert result is None


def test_discover_changed_files_includes_untracked_files_for_head(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd == ["git", "diff", "--name-only", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "src/bernstein/core/models.py\n", "")
        if cmd == ["git", "diff", "--name-only", "--cached"]:
            return subprocess.CompletedProcess(cmd, 0, "tests/unit/test_models.py\n", "")
        if cmd == ["git", "ls-files", "--others", "--exclude-standard"]:
            return subprocess.CompletedProcess(cmd, 0, "src/bernstein/core/new_module.py\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(run_tests_module.subprocess, "run", fake_run)

    assert run_tests_module.discover_changed_files("HEAD") == [
        "src/bernstein/core/models.py",
        "src/bernstein/core/new_module.py",
        "tests/unit/test_models.py",
    ]
    assert ["git", "ls-files", "--others", "--exclude-standard"] in calls
