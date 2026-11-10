"""Unit tests for the async quality gate runner."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.gate_runner import GatePipelineStep, GateResult, GateRunner, normalize_gate_condition
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import QualityGatesConfig


def _make_task(*, owned_files: list[str] | None = None) -> Task:
    return Task(
        id="T-gates-1",
        title="Quality gates task",
        description="Exercise the gate runner.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        owned_files=owned_files or [],
    )


def test_normalize_legacy_condition() -> None:
    assert normalize_gate_condition("changed_files.any('.py')") == "python_changed"


def test_parallel_execution_preserves_pipeline_order(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "module.py").write_text("print('ok')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[
            GatePipelineStep(name="lint", required=True, condition="python_changed"),
            GatePipelineStep(name="type_check", required=True, condition="python_changed"),
        ],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/module.py"])

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return True, "ok", 0

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert max_active >= 2
    assert [result.name for result in report.results] == ["lint", "type_check"]


def test_changed_file_resolution_prefers_owned_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "owned.py").write_text("print('owned')\n", encoding="utf-8")
    (src / "fallback.py").write_text("print('fallback')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/owned.py", "missing.py"])

    with patch("bernstein.core.quality.quality_gates._run_command", return_value=(True, "ok", 0)):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.changed_files == ["src/owned.py"]


def test_changed_file_resolution_uses_git_diff_fallback(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "fallback.py").write_text("print('fallback')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    with (
        patch.object(GateRunner, "_git_diff_changed_files", return_value=["src/fallback.py"]),
        patch("bernstein.core.quality.quality_gates._run_command", return_value=(True, "ok", 0)),
    ):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.changed_files == ["src/fallback.py"]


def test_timeout_blocks_required_gate(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    with patch("bernstein.core.quality.quality_gates._run_command", return_value=(False, "Timed out after 30s", -1)):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert not report.overall_pass
    assert report.results[0].status == "timeout"
    assert report.results[0].blocked


def test_timeout_does_not_block_optional_gate(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=False, condition="always")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    with patch("bernstein.core.quality.quality_gates._run_command", return_value=(False, "Timed out after 30s", -1)):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.overall_pass
    assert report.results[0].status == "timeout"
    assert not report.results[0].blocked


def test_non_required_fail_does_not_block(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=False, condition="always")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    with patch("bernstein.core.quality.quality_gates._run_command", return_value=(False, "lint failed", 1)):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.overall_pass
    assert report.results[0].status == "fail"
    assert not report.results[0].blocked


def _test_expansion_report(
    tmp_path: Path, files: dict[str, str], changed: list[str], *, required: bool = False
) -> tuple[GateResult, bool]:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    step = GatePipelineStep(name="test_expansion", required=required, condition="python_changed")
    config = QualityGatesConfig(pipeline=[step], cache_enabled=False)
    report = asyncio.run(GateRunner(config, tmp_path).run_all(_make_task(owned_files=changed), tmp_path))
    return report.results[0], report.overall_pass


@pytest.mark.parametrize("required", [False, True])
def test_expansion_unimported_placeholder_is_uncovered(tmp_path: Path, required: bool) -> None:
    result, overall_pass = _test_expansion_report(
        tmp_path,
        {
            "src/demo/feature.py": "def new_secure_handler(payload: dict) -> bool:\n    return payload.get('auth') is True\n",
            "tests/unit/test_feature.py": "def test_placeholder():\n    assert 1 + 1 == 2\n",
        },
        ["src/demo/feature.py", "tests/unit/test_feature.py"],
        required=required,
    )
    assert result.status == "fail"
    assert result.blocked is required
    assert overall_pass is (not required)
    assert result.metadata["uncovered_files"] == ["src/demo/feature.py"]
    needs_coverage = tmp_path / ".sdd/runtime/needs_coverage.json"
    assert '"source_file": "src/demo/feature.py"' in needs_coverage.read_text(encoding="utf-8")


@pytest.mark.parametrize("test_import", ["from demo.feature import new_secure_handler\n", "import demo.feature\n"])
def test_expansion_changed_test_imports_changed_module(tmp_path: Path, test_import: str) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/feature.py": "def new_secure_handler():\n    return True\n",
            "tests/unit/test_feature.py": test_import + "def test_placeholder():\n    assert True\n",
        },
        ["src/demo/feature.py", "tests/unit/test_feature.py"],
    )
    assert result.status == "pass"
    assert result.metadata.get("uncovered_files", []) == []


def test_expansion_changed_test_imports_transitive_source_module(tmp_path: Path) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/feature.py": "def new_secure_handler():\n    return True\n",
            "src/demo/bridge.py": "from demo.feature import new_secure_handler\n",
            "tests/unit/test_feature.py": "from demo.bridge import new_secure_handler\ndef test_placeholder():\n    assert True\n",
        },
        ["src/demo/feature.py", "tests/unit/test_feature.py"],
    )
    assert result.status == "pass"


def test_expansion_existing_unchanged_test_does_not_prove_import(tmp_path: Path) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/feature.py": "def new_secure_handler():\n    return True\n",
            "tests/unit/test_feature.py": "def test_placeholder():\n    assert 'demo.feature'\n",
            "tests/unit/test_existing.py": "from demo.feature import new_secure_handler\n",
        },
        ["src/demo/feature.py", "tests/unit/test_feature.py"],
    )
    assert result.status == "fail"
    assert result.metadata["uncovered_files"] == ["src/demo/feature.py"]


def test_expansion_only_unreachable_production_module_is_reported(tmp_path: Path) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/used.py": "def changed():\n    return True\n",
            "src/demo/missing.py": "def changed():\n    return True\n",
            "tests/unit/test_used.py": "import demo.used\ndef test_placeholder():\n    assert True\n",
        },
        ["src/demo/used.py", "src/demo/missing.py", "tests/unit/test_used.py"],
    )
    assert result.status == "fail"
    assert result.metadata["uncovered_files"] == ["src/demo/missing.py"]


def test_expansion_test_only_change_is_skipped(tmp_path: Path) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {"tests/unit/test_feature.py": "def test_placeholder():\n    assert True\n"},
        ["tests/unit/test_feature.py"],
    )
    assert result.status == "skipped"


def test_expansion_flat_fixture_and_suffix_test_are_supported(tmp_path: Path) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "feature.py": "def new_secure_handler():\n    return True\n",
            "feature_test.py": "from feature import new_secure_handler\ndef test_placeholder():\n    assert True\n",
        },
        ["feature.py", "feature_test.py"],
    )
    assert result.status == "pass"


@pytest.mark.parametrize(
    ("test_import", "bridge_import", "expected"),
    [
        ("from demo import feature", "", "pass"),
        ("from demo import bridge", "from demo import feature\n", "pass"),
        ("from demo import bridge", "from . import feature\n", "pass"),
        ("import demo", "", "fail"),
        ("from demo import bridge", "", "fail"),
    ],
)
def test_expansion_package_child_imports(tmp_path: Path, test_import: str, bridge_import: str, expected: str) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/__init__.py": "",
            "src/demo/feature.py": "def target():\n    return True\n",
            "src/demo/bridge.py": bridge_import,
            "tests/unit/test_feature.py": f"{test_import}\ndef test_placeholder():\n    assert True\n",
        },
        ["src/demo/feature.py", "tests/unit/test_feature.py"],
    )
    assert result.status == expected
    assert result.metadata.get("uncovered_files", []) == ([] if expected == "pass" else ["src/demo/feature.py"])


def test_expansion_importing_child_reaches_changed_package_init(tmp_path: Path) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/__init__.py": "VALUE = 1\n",
            "src/demo/feature.py": "def target():\n    return True\n",
            "tests/unit/test_feature.py": "import demo.feature\ndef test_placeholder():\n    assert True\n",
        },
        ["src/demo/__init__.py", "tests/unit/test_feature.py"],
    )
    assert result.status == "pass"


@pytest.mark.parametrize(
    "test_import",
    ["from scripts.feature import target", "from scripts import feature", "from scripts.bridge import target"],
)
def test_expansion_mixed_src_and_root_modules(tmp_path: Path, test_import: str) -> None:
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/unrelated.py": "def other():\n    return True\n",
            "scripts/feature.py": "def target():\n    return True\n",
            "scripts/bridge.py": "from scripts.feature import target\n",
            "tests/unit/test_feature.py": f"{test_import}\ndef test_placeholder():\n    assert True\n",
        },
        ["scripts/feature.py", "tests/unit/test_feature.py"],
    )
    assert result.status == "pass"


@pytest.mark.parametrize(
    ("changed_source", "bridge", "bridge_import", "expected"),
    [
        ("src/demo/feature.py", "scripts/bridge.py", "from demo.feature import target", "pass"),
        ("feature.py", "bridge.py", "from feature import target", "pass"),
        ("src/demo/feature.py", "scripts/bridge.py", "from demo.unrelated import target", "fail"),
    ],
)
def test_expansion_unchanged_root_bridge(
    tmp_path: Path, changed_source: str, bridge: str, bridge_import: str, expected: str
) -> None:
    module = bridge[:-3].replace("/", ".")
    result, _ = _test_expansion_report(
        tmp_path,
        {
            "src/demo/unrelated.py": "def target():\n    return True\n",
            changed_source: "def target():\n    return True\n",
            bridge: bridge_import + "\n",
            "tests/unit/test_feature.py": f"from {module} import target\ndef test_active():\n    assert target()\n",
        },
        [changed_source, "tests/unit/test_feature.py"],
    )
    assert result.status == expected
    assert result.metadata.get("uncovered_files", []) == ([] if expected == "pass" else [changed_source])


def test_expansion_does_not_cache_stale_transitive_imports(tmp_path: Path) -> None:
    files = {
        "src/demo/feature.py": "def target():\n    return True\n",
        "src/demo/bridge.py": "from demo.feature import target\n",
        "tests/unit/test_feature.py": "from demo.bridge import target\ndef test_placeholder():\n    assert True\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    step = GatePipelineStep(name="test_expansion", required=False, condition="python_changed")
    runner = GateRunner(QualityGatesConfig(pipeline=[step], cache_enabled=True), tmp_path)
    changed = ["src/demo/feature.py", "tests/unit/test_feature.py"]
    first = asyncio.run(runner.run_gate(step, _make_task(), tmp_path, changed))
    assert first.status == "pass" and not first.cached

    (tmp_path / "src/demo/bridge.py").write_text("VALUE = 1\n", encoding="utf-8")
    second = asyncio.run(runner.run_gate(step, _make_task(), tmp_path, changed))
    assert second.status == "fail" and not second.cached
    assert second.metadata["uncovered_files"] == ["src/demo/feature.py"]


def test_cache_hit_and_invalidation_by_content_hash(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    target = src / "cache_me.py"
    target.write_text("print('one')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        cache_enabled=True,
    )
    task = _make_task(owned_files=["src/cache_me.py"])

    run_count = 0

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal run_count
        run_count += 1
        return True, "ok", 0

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
        report_one = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))
        report_two = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))
        target.write_text("print('two')\n", encoding="utf-8")
        report_three = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))

    assert run_count == 2
    assert not report_one.results[0].cached
    assert report_two.results[0].cached
    assert not report_three.results[0].cached


def test_timeout_and_bypass_are_not_cached(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    target = src / "skip_me.py"
    target.write_text("print('skip')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        allow_bypass=True,
        cache_enabled=True,
    )
    task = _make_task(owned_files=["src/skip_me.py"])

    timeout_count = 0

    def fake_timeout(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal timeout_count
        timeout_count += 1
        return False, "Timed out after 5s", -1

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_timeout):
        asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))
        asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))

    assert timeout_count == 2

    command_count = 0

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal command_count
        command_count += 1
        return True, "ok", 0

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
        asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path, skip_gates=["lint"], bypass_reason="manual"))
        report = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))

    assert command_count == 1
    assert not report.results[0].cached


def test_bypass_denied_when_disabled(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
        allow_bypass=False,
    )
    runner = GateRunner(config, tmp_path)

    with pytest.raises(ValueError, match="bypass is disabled"):
        asyncio.run(runner.run_all(_make_task(), tmp_path, skip_gates=["lint"]))


# ---------------------------------------------------------------------------
# Auto-format gate
# ---------------------------------------------------------------------------


def _make_auto_format_runner(tmp_path: Path, *, python_cmd: str = "ruff format") -> GateRunner:
    config = QualityGatesConfig(
        auto_format=True,
        auto_format_python_command=python_cmd,
        pipeline=[GatePipelineStep(name="auto_format", required=False, condition="any_changed")],
        cache_enabled=False,
    )
    return GateRunner(config, tmp_path)


def test_auto_format_passes_and_reports_reformatted_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_format gate always passes and reports how many files were reformatted."""
    import subprocess

    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    runner = _make_auto_format_runner(tmp_path)
    task = _make_task(owned_files=["a.py"])

    fake_proc = subprocess.CompletedProcess(
        args=["ruff", "format", "a.py"],
        returncode=0,
        stdout="1 file reformatted",
        stderr="",
    )

    with patch("subprocess.run", return_value=fake_proc):
        report = asyncio.run(runner.run_all(task, tmp_path))

    result = report.results[0]
    assert result.name == "auto_format"
    assert result.status == "pass"
    assert result.blocked is False
    assert "Python" in result.details
    assert "reformatted" in result.details


def test_auto_format_skips_when_formatter_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_format skips a language when its formatter binary is not on PATH."""
    import shutil

    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    runner = _make_auto_format_runner(tmp_path, python_cmd="nonexistent-fmt")
    task = _make_task(owned_files=["a.py"])

    original_which = shutil.which

    def fake_which(name: str) -> str | None:
        if name == "nonexistent-fmt":
            return None
        return original_which(name)

    monkeypatch.setattr(shutil, "which", fake_which)

    report = asyncio.run(runner.run_all(task, tmp_path))

    result = report.results[0]
    assert result.status == "pass"
    assert result.blocked is False
    assert "not found" in result.details


def test_auto_format_skips_when_no_changed_files(tmp_path: Path) -> None:
    """auto_format gate skips cleanly when no changed files are present."""
    config = QualityGatesConfig(
        auto_format=True,
        pipeline=[GatePipelineStep(name="auto_format", required=False, condition="any_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=[])

    report = asyncio.run(runner.run_all(task, tmp_path))

    result = report.results[0]
    assert result.status == "skipped"
    assert result.blocked is False


def test_auto_format_appears_before_lint_in_default_pipeline(tmp_path: Path) -> None:
    """auto_format is inserted before lint in the default pipeline."""
    from bernstein.core.gate_runner import build_default_pipeline

    config = QualityGatesConfig(auto_format=True, lint=True)
    pipeline = build_default_pipeline(config)
    names = [step.name for step in pipeline]
    assert "auto_format" in names
    assert "lint" in names
    assert names.index("auto_format") < names.index("lint")


# ---------------------------------------------------------------------------
# Incremental type-check with dependent expansion
# ---------------------------------------------------------------------------


def test_type_check_command_includes_transitive_importers(tmp_path: Path) -> None:
    """A signature change in models.py causes pyright to also check service.py."""
    src = tmp_path / "src"
    (src / "demo").mkdir(parents=True)
    (src / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (src / "demo" / "models.py").write_text("class Model:\n    name: str\n", encoding="utf-8")
    (src / "demo" / "service.py").write_text(
        "from demo.models import Model\n\ndef use() -> Model:\n    return Model()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="type_check", required=True, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/demo/models.py"])

    captured_commands: list[str] = []

    def fake_run(command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        captured_commands.append(command)
        return True, "ok", 0

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
        asyncio.run(runner.run_all(task, tmp_path))

    assert len(captured_commands) == 1
    cmd = captured_commands[0]
    assert "models.py" in cmd
    # service.py imports models.py - it must be included in the type-check scope
    assert "service.py" in cmd


def test_type_check_command_falls_back_when_dependency_info_unavailable(tmp_path: Path) -> None:
    """When dependency index is empty, pyright still runs on the changed files only."""
    src = tmp_path / "src"
    (src / "demo").mkdir(parents=True)
    (src / "demo" / "models.py").write_text("class Model: pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="type_check", required=True, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/demo/models.py"])

    captured_commands: list[str] = []

    def fake_run(command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        captured_commands.append(command)
        return True, "ok", 0

    with (
        patch("bernstein.core.test_impact.TestImpactAnalyzer") as mock_analyzer_cls,
        patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run),
    ):
        mock_analyzer_cls.side_effect = RuntimeError("index unavailable")
        asyncio.run(runner.run_all(task, tmp_path))

    assert len(captured_commands) == 1
    assert "models.py" in captured_commands[0]


def test_type_check_command_no_extra_files_when_no_importers(tmp_path: Path) -> None:
    """A leaf module with no importers is type-checked alone (no extra files added)."""
    src = tmp_path / "src"
    (src / "demo").mkdir(parents=True)
    (src / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (src / "demo" / "utils.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
    (src / "demo" / "other.py").write_text("def thing() -> int:\n    return 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="type_check", required=True, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/demo/utils.py"])

    captured_commands: list[str] = []

    def fake_run(command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        captured_commands.append(command)
        return True, "ok", 0

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
        asyncio.run(runner.run_all(task, tmp_path))

    assert len(captured_commands) == 1
    cmd = captured_commands[0]
    assert "utils.py" in cmd
    # other.py does not import utils.py - it must not be included
    assert "other.py" not in cmd


def test_dead_code_gate_runs_to_completion_instead_of_crashing(tmp_path: Path) -> None:
    """Regression for #5572: the dead-code gate must not raise AttributeError.

    ``GateRunner._run_dead_code_gate_sync`` called ``self._build_dead_code_result``,
    a method ``GateRunner`` never defined or inherited -- ``GateRunnerCommandsMixin``
    defines it, but nothing composes that mixin into ``GateRunner`` (confirmed:
    ``GateRunner.__mro__`` is just ``(GateRunner, object)``). The gate is disabled
    by default (``dead_code_check=False``), which is why this went unnoticed: any
    operator who turned it on would have hit this on the very first run. This test
    fails before the fix (an unhandled ``AttributeError`` propagates out of
    ``run_all``) and passes after.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "module.py").write_text("def f() -> int:\n    return 1\n", encoding="utf-8")

    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="dead_code", required=False, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/module.py"])

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str, int]:
        return True, "(no output)", 0

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.gates_run == ["dead_code"]
    (result,) = report.results
    assert result.status in ("pass", "fail", "warn")


def test_every_valid_gate_name_is_dispatchable(tmp_path: Path) -> None:
    """General invariant behind #6156: every name in ``VALID_GATE_NAMES``
    must resolve to a real handler in ``GateRunner._execute_gate`` and never
    fall through to the plugin-registry fallback, which raises
    ``ValueError: Unsupported gate name`` for any built-in name (plugin
    registration refuses names that collide with ``VALID_GATE_NAMES``).

    Each handler is stubbed so this test isolates *dispatch* (did routing
    find a handler) from gate *behaviour* (did the gate's own logic pass or
    fail). Exercising every gate's real logic here would mean spinning up
    subprocesses (ruff/mypy/pytest/bandit/mutmut) and -- per #6156's own
    report -- risking a real network call for ``intent_verification``
    (``OPENROUTER_API_KEY_PAID``). That is exactly the flakiness this
    regression test must not introduce.
    """
    from bernstein.core.gate_runner import GateResult

    from bernstein.core.quality.gate_pipeline import VALID_GATE_NAMES

    config = QualityGatesConfig(cache_enabled=False)
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    stub_result = GateResult(
        name="stub",
        status="pass",
        required=False,
        blocked=False,
        cached=False,
        duration_ms=0,
        details="stubbed for dispatch test",
        metadata={},
    )

    def _sync_stub(*_args: object, **_kwargs: object) -> GateResult:
        return stub_result

    async def _async_stub(*_args: object, **_kwargs: object) -> GateResult:
        return stub_result

    # Every handler name referenced by GateRunner._execute_gate's dispatch
    # tables (`_sync_cf_gates` / `_sync_no_cf_gates` / `_async_gates`).
    # Instance-attribute assignment shadows the bound method, and the
    # dispatch dicts look up `self.<name>` fresh on every call, so this
    # reaches the exact same routing code the real run does.
    sync_handler_names = [
        "_run_auto_format_gate_sync",
        "_run_complexity_gate_sync",
        "_run_dead_code_gate_sync",
        "_run_comment_quality_gate_sync",
        "_run_import_cycle_gate_sync",
        "_run_coverage_delta_gate_sync",
        "_run_merge_conflict_gate_sync",
        "_run_large_file_gate_sync",
        "_run_run_config_gate_sync",
        "_run_benchmark_gate_sync",
        "_run_migration_reversibility_gate_sync",
        "_run_test_expansion_gate_sync",
    ]
    async_handler_names = [
        "_execute_lint_gate",
        "_execute_type_check_gate",
        "_run_tests_gate",
        "_execute_security_scan_gate",
        "_execute_scan_gate",
        "_execute_mutation_gate",
        "_execute_intent_gate",
        "_execute_dep_audit_gate",
        "_run_integration_test_gen_gate",
        "_run_review_rubric_gate",
        "_run_behavior_probe_gate",
    ]
    for name in sync_handler_names:
        setattr(runner, name, _sync_stub)
    for name in async_handler_names:
        setattr(runner, name, _async_stub)

    for name in sorted(VALID_GATE_NAMES):
        step = GatePipelineStep(name=name, required=False, condition="always")
        result = asyncio.run(runner.run_gate(step, task, tmp_path, []))
        assert result is stub_result, f"{name}: did not reach a stubbed dispatch handler"


def _dead_code_report(tmp_path: Path, *, output: str, exit_code: int, required: bool = False):
    """Run the dead-code gate with one scripted command result."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "module.py").write_text("def f() -> int:\n    return 1\n", encoding="utf-8")

    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="dead_code", required=required, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/module.py"])

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str, int]:
        return exit_code == 0, output, exit_code

    with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
        return asyncio.run(runner.run_all(task, tmp_path))


def test_dead_code_gate_reports_a_missing_tool_as_command_not_found(tmp_path: Path) -> None:
    """Regression for #5869: an absent vulture is not a finding about the code.

    The gate handed the command's ``(ok, output)`` straight to ``_build_dead_code_result``, so a
    missing tool produced ``status="fail"`` -- the same verdict as "dead code found". vulture is
    not a project dependency, so that is the state of a fresh checkout rather than an edge case,
    and anything counting gate failures as findings counted an uninstalled tool as a catch.
    """
    report = _dead_code_report(
        tmp_path,
        output="python.exe: No module named vulture",
        # `python -m <missing>` exits 1, NOT 127 -- which is why the exit-code rule the lint,
        # import-cycle and complexity gates already use never matched here.
        exit_code=1,
    )

    (result,) = report.results
    assert result.status == "command_not_found"
    assert result.status != "fail"


def test_the_missing_tool_verdict_names_the_tool(tmp_path: Path) -> None:
    """An operator reading this has to be sent to `pip install`, not to their own code."""
    report = _dead_code_report(
        tmp_path,
        output="python.exe: No module named vulture",
        exit_code=1,
    )

    (result,) = report.results
    assert "vulture" in result.details


def test_a_shell_reporting_127_is_recognised_too(tmp_path: Path) -> None:
    """The other shape: a bare executable the shell cannot find."""
    report = _dead_code_report(tmp_path, output="vulture: command not found", exit_code=127)

    assert report.results[0].status == "command_not_found"


def test_a_required_dead_code_gate_that_could_not_run_still_blocks(tmp_path: Path) -> None:
    """The status changes; the safety does not.

    A gate that never ran has cleared nothing, so a required one must still block. What #5869 is
    about is the REASON an operator is shown, not whether the task proceeds.
    """
    report = _dead_code_report(
        tmp_path,
        output="python.exe: No module named vulture",
        exit_code=1,
        required=True,
    )

    (result,) = report.results
    assert result.status == "command_not_found"
    assert result.blocked is True


def test_a_real_vulture_finding_is_still_a_failure(tmp_path: Path) -> None:
    """The guard that keeps the fix from swallowing the gate.

    An exemption keyed on the message must not fire on output that merely mentions a module.
    """
    report = _dead_code_report(
        tmp_path,
        output="src/module.py:1: unused function 'f' (60% confidence)",
        exit_code=1,
    )

    assert report.results[0].status != "command_not_found"


def test_a_clean_run_is_still_a_pass(tmp_path: Path) -> None:
    report = _dead_code_report(tmp_path, output="(no output)", exit_code=0)

    assert report.results[0].status in ("pass", "warn")


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _run_branch_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose only branch is ``fleet/work`` (no ``main``), holding a file that
    trips the DLP license check, and an agent worktree that adds one new file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "fleet/work")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "NOTICE.md").write_text("Vendored notes. All rights reserved.\n", encoding="utf-8")
    _git(repo, "add", "NOTICE.md")
    _git(repo, "commit", "-m", "seed")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "agent/A-1", str(worktree))
    (worktree / "src").mkdir()
    (worktree / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "src/new.py")
    _git(worktree, "commit", "-m", "agent: add new.py")
    return repo, worktree


def test_missing_base_ref_falls_back_to_the_run_branch(tmp_path: Path) -> None:
    """The default base ``main`` does not exist here: the diff is taken against the branch
    the agent forked from and merges into, not dropped in favour of a whole-tree scan."""
    repo, worktree = _run_branch_repo(tmp_path)
    runner = GateRunner(QualityGatesConfig(cache_enabled=False), repo)

    assert runner._base_ref == "fleet/work"
    assert runner._resolve_changed_files_sync(_make_task(), worktree) == ["src/new.py"]


def test_existing_base_ref_is_kept(tmp_path: Path) -> None:
    repo, _ = _run_branch_repo(tmp_path)
    _git(repo, "branch", "main")

    assert GateRunner(QualityGatesConfig(cache_enabled=False), repo)._base_ref == "main"


def test_dlp_gate_reads_only_the_agent_change_when_base_ref_is_missing(tmp_path: Path) -> None:
    """Text already on the run branch must not block an agent whose change is clean."""
    repo, worktree = _run_branch_repo(tmp_path)
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="dlp_scan", required=True)],
        cache_enabled=False,
    )

    report = asyncio.run(GateRunner(config, repo).run_all(_make_task(), worktree))

    assert report.changed_files == ["src/new.py"]
    assert [(r.name, r.status, r.blocked) for r in report.results] == [("dlp_scan", "pass", False)]
