"""Unit tests for #2959: Generic Python-invoked agent-runtime adapter."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters._contract import (
    STRATEGY_MATRIX,
    DangerousModeStrategy,
    EventChannel,
    OutputMode,
    ResumeStrategy,
    undeclared_strategies,
)
from bernstein.adapters.python_runtime import PythonRuntimeAdapter, RuntimeConfigError
from bernstein.adapters.registry import get_adapter, selectable_adapter_names

RUNNER = Path(__file__).resolve().parents[3] / "src" / "bernstein" / "adapters" / "python_runtime_runner.py"


def test_python_runtime_adapter_registered_and_strategy_matrix() -> None:
    assert "python_runtime" in selectable_adapter_names()
    adapter = get_adapter("python_runtime")
    assert isinstance(adapter, PythonRuntimeAdapter)

    strategy = STRATEGY_MATRIX.get("python_runtime")
    assert strategy is not None
    assert strategy.resume == ResumeStrategy.UNSUPPORTED
    assert strategy.dangerous_mode == DangerousModeStrategy.ALWAYS_ON
    assert strategy.event_channel == EventChannel.STREAM_JSON
    assert strategy.output_mode == OutputMode.GIT_DIFF

    assert "python_runtime" not in undeclared_strategies(selectable_adapter_names())


def test_adapter_instance_resolves_its_declared_strategy() -> None:
    """The live adapter must resolve the declared row, not the safe default.

    ``name()`` lowers to ``"pythonruntime"``, which is not a matrix key, so
    without an explicit ``registry_name`` every consumer reading strategy off
    the instance (``commit_completion`` reads ``output_mode`` this way) would
    silently get ``DEFAULT_ADAPTER_STRATEGY`` - dangerous mode "unsupported",
    event channel "text-signals" - and the declaration would be decorative.
    """
    adapter = PythonRuntimeAdapter()
    assert adapter._derive_session_namespace() == "python_runtime"
    assert adapter.strategy() == STRATEGY_MATRIX["python_runtime"]
    assert adapter.strategy().dangerous_mode == DangerousModeStrategy.ALWAYS_ON
    assert adapter.strategy().event_channel == EventChannel.STREAM_JSON


def test_python_runtime_plugin_info() -> None:
    adapter = PythonRuntimeAdapter()
    info = adapter.plugin_info()
    assert info.name == "python_runtime"
    assert info.version == "1.0.0"


def test_detect_tier_returns_none_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic runtime cannot attribute a provider tier, so it claims none.

    The adapter-wide contract is "``detect_tier()`` returns ``ApiTierInfo`` or
    ``None``, never raises" (``tests/unit/test_adapter_consistency.py``). This
    pins the env-dependent branch that check only exercises when a provider key
    happens to be present in the environment.
    """
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(key, "sk-test")
    assert PythonRuntimeAdapter().detect_tier() is None


def test_binary_resolution_matches_the_contract() -> None:
    """``bernstein doctor`` must probe ``python``, not ``python_runtime``."""
    from bernstein.adapters._contract import ContractSpec
    from bernstein.adapters.report import _binary_for_adapter

    assert _binary_for_adapter("python_runtime") == "python"
    assert ContractSpec.load("python_runtime").binary == "python"


# ---------------------------------------------------------------------------
# spawn(): argv construction and configuration refusals
# ---------------------------------------------------------------------------


def test_python_runtime_spawn(tmp_path: Path) -> None:
    adapter = PythonRuntimeAdapter()
    model_cfg = ModelConfig(model="gpt-4o", effort="normal")

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        result = adapter.spawn(
            prompt="Run custom python agent",
            workdir=tmp_path,
            model_config=model_cfg,
            session_id="py-task-1",
            mcp_config={"runtime_module": "custom_agent", "runtime_entrypoint": "run_agent"},
            timeout_seconds=0,
        )

        assert result.pid == 9999
        assert mock_popen.called

        cmd_list = mock_popen.call_args[0][0]
        assert "python_runtime_runner.py" in str(cmd_list)
        assert "--prompt" in cmd_list
        assert "Run custom python agent" in cmd_list
        assert "--runtime-module" in cmd_list
        assert "custom_agent" in cmd_list
        assert "--runtime-entrypoint" in cmd_list
        assert "run_agent" in cmd_list


def test_spawn_boundary_matches_the_cross_adapter_process_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restate the shared spawn-boundary contract, which now skips this adapter.

    ``tests/property/test_adapter_spawn_bughunt.py`` pins the process boundary
    for every adapter - argv is ``list[str]``, never ``shell=True``, ``env=``
    explicit, ``cwd=`` the worktree, ``SpawnResult.proc`` the live handle - and
    ``tests/unit/test_adapter_consistency.py`` pins the ``SpawnResult`` shape.
    All three call ``spawn()`` with no ``mcp_config``, which this adapter now
    refuses, so they ``skip`` for ``python_runtime`` instead of failing. Nothing
    else in the tree checks this adapter's boundary; pin it here so the refusal
    does not silently trade a contract for a skip.

    The env assertion is the load-bearing half: the adapter hands the child an
    allowlisted environment, so an unrelated orchestrator secret must not reach
    a runtime that is imported and called with no permission surface.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider")
    monkeypatch.setenv("UNRELATED_DEPLOY_SECRET", "must-not-propagate")
    adapter = PythonRuntimeAdapter()

    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=1234)
        result = adapter.spawn(
            prompt="probe",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="normal"),
            session_id="py-task-6",
            mcp_config={"runtime_module": "custom_agent"},
            timeout_seconds=0,
        )

    args = mock_popen.call_args.args
    kwargs = mock_popen.call_args.kwargs

    cmd = args[0] if args else kwargs.get("args")
    assert isinstance(cmd, list)
    assert all(isinstance(token, str) for token in cmd)
    assert not kwargs.get("shell", False)

    assert isinstance(kwargs.get("env"), dict)
    assert "UNRELATED_DEPLOY_SECRET" not in kwargs["env"]
    assert kwargs["env"].get("OPENAI_API_KEY") == "sk-provider"

    assert Path(kwargs["cwd"]) == tmp_path
    assert result.proc is mock_popen.return_value
    assert isinstance(result.log_path, Path)


def test_spawn_forwards_system_addendum_into_the_prompt(tmp_path: Path) -> None:
    """A Python runtime has no system channel, so the addendum rides the prompt.

    Dropping it would silently strip protocol-critical orchestration
    instructions from every task run through this adapter.
    """
    adapter = PythonRuntimeAdapter()

    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=4321)
        adapter.spawn(
            prompt="Do the task",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="normal"),
            session_id="py-task-2",
            mcp_config={"runtime_module": "custom_agent"},
            system_addendum="ALWAYS emit BERNSTEIN:DONE when finished",
            timeout_seconds=0,
        )

    cmd_list = mock_popen.call_args[0][0]
    prompt_arg = cmd_list[cmd_list.index("--prompt") + 1]
    assert "Do the task" in prompt_arg
    assert "ALWAYS emit BERNSTEIN:DONE when finished" in prompt_arg


def test_spawn_defaults_the_entrypoint_when_unset(tmp_path: Path) -> None:
    adapter = PythonRuntimeAdapter()

    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=4322)
        adapter.spawn(
            prompt="p",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="normal"),
            session_id="py-task-3",
            mcp_config={"runtime_module": "custom_agent"},
            timeout_seconds=0,
        )

    cmd_list = mock_popen.call_args[0][0]
    assert cmd_list[cmd_list.index("--runtime-entrypoint") + 1] == "chat"


@pytest.mark.parametrize(
    "mcp_config",
    [
        pytest.param(None, id="no-config"),
        pytest.param({}, id="empty-config"),
        pytest.param({"runtime_entrypoint": "run"}, id="entrypoint-without-module"),
    ],
)
def test_spawn_refuses_without_a_runtime_module(tmp_path: Path, mcp_config: dict[str, object] | None) -> None:
    """No configured runtime means nothing to run - refuse instead of faking it.

    The previous fallback started a worker that printed
    ``{"status": "completed"}`` without touching the worktree, so a task with a
    missing runtime config reported success having done nothing.
    """
    adapter = PythonRuntimeAdapter()

    with patch("subprocess.Popen") as mock_popen, pytest.raises(RuntimeConfigError):
        adapter.spawn(
            prompt="p",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="normal"),
            session_id="py-task-4",
            mcp_config=mcp_config,
            timeout_seconds=0,
        )
    assert not mock_popen.called


@pytest.mark.parametrize(
    "mcp_config",
    [
        pytest.param({"runtime_module": None}, id="module-none"),
        pytest.param({"runtime_module": ["a", "b"]}, id="module-list"),
        pytest.param({"runtime_module": "  "}, id="module-blank"),
        pytest.param({"runtime_module": "m", "runtime_entrypoint": []}, id="entrypoint-list"),
        pytest.param({"runtime_module": "m", "runtime_entrypoint": ""}, id="entrypoint-empty"),
    ],
)
def test_spawn_refuses_non_string_runtime_config(tmp_path: Path, mcp_config: dict[str, object]) -> None:
    """``mcp_config`` is ``dict[str, Any]``; unvalidated values become argv text.

    ``str(None)`` is the importable-looking module name ``"None"`` and
    ``str([])`` the attribute name ``"[]"``; both used to reach the runner and
    return a started session.
    """
    adapter = PythonRuntimeAdapter()

    with patch("subprocess.Popen") as mock_popen, pytest.raises(RuntimeConfigError):
        adapter.spawn(
            prompt="p",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="normal"),
            session_id="py-task-5",
            mcp_config=mcp_config,
            timeout_seconds=0,
        )
    assert not mock_popen.called


# ---------------------------------------------------------------------------
# The runner, executed for real
# ---------------------------------------------------------------------------


def _write_runtime(tmp_path: Path, body: str) -> Path:
    """Write an importable runtime module and return its parent directory."""
    pkg_dir = tmp_path / "runtimes"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "fake_runtime.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return pkg_dir


def _run_runner(pkg_dir: Path, workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), "--workdir", str(workdir), *args],
        capture_output=True,
        text=True,
        cwd=workdir,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(pkg_dir)},
        check=False,
    )


def _events(stdout: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_runner_drives_a_real_entrypoint_end_to_end(tmp_path: Path) -> None:
    """Import a real module, call it, and prove prompt/model/workdir arrive."""
    pkg_dir = _write_runtime(
        tmp_path,
        """
        def chat(*, prompt, model, workdir):
            (workdir / "written-by-runtime.txt").write_text(prompt, encoding="utf-8")
            return f"{model}:{prompt}"
        """,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    proc = _run_runner(
        pkg_dir,
        workdir,
        "--prompt",
        "make it so",
        "--model",
        "gpt-4o",
        "--runtime-module",
        "fake_runtime",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    events = _events(proc.stdout)
    assert events[0]["event"] == "start"
    assert events[0]["prompt"] == "make it so"
    assert events[-1] == {"event": "result", "output": "gpt-4o:make it so", "status": "completed"}
    assert (workdir / "written-by-runtime.txt").read_text(encoding="utf-8") == "make it so"


def test_runner_honours_a_custom_entrypoint(tmp_path: Path) -> None:
    pkg_dir = _write_runtime(
        tmp_path,
        """
        def chat(*, prompt, model, workdir):
            return "wrong entrypoint"

        def run_agent(*, prompt, model, workdir):
            return "right entrypoint"
        """,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    proc = _run_runner(
        pkg_dir,
        workdir,
        "--prompt",
        "p",
        "--runtime-module",
        "fake_runtime",
        "--runtime-entrypoint",
        "run_agent",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _events(proc.stdout)[-1]["output"] == "right entrypoint"


@pytest.mark.parametrize(
    ("body", "extra_args", "expected_fragment"),
    [
        pytest.param(
            "def chat(*, prompt, model, workdir):\n    return 'ok'\n",
            ("--runtime-module", "no_such_runtime_module"),
            "Failed importing no_such_runtime_module",
            id="import-error",
        ),
        pytest.param(
            "VALUE = 1\n",
            ("--runtime-module", "fake_runtime"),
            "Entrypoint 'chat' not found",
            id="missing-entrypoint",
        ),
        pytest.param(
            "chat = 3\n",
            ("--runtime-module", "fake_runtime"),
            "Entrypoint 'chat' not found",
            id="non-callable-entrypoint",
        ),
        pytest.param(
            "def chat(*, prompt, model, workdir):\n    raise RuntimeError('boom')\n",
            ("--runtime-module", "fake_runtime"),
            "boom",
            id="entrypoint-raised",
        ),
    ],
)
def test_runner_exits_non_zero_on_every_failure_path(
    tmp_path: Path,
    body: str,
    extra_args: tuple[str, ...],
    expected_fragment: str,
) -> None:
    """A failed run must be visible to a supervisor that only reads the exit code.

    Every one of these paths previously emitted an ``error`` event and then
    returned normally, so the worker exited 0 and the orchestrator recorded a
    successful run.
    """
    pkg_dir = _write_runtime(tmp_path, body)
    workdir = tmp_path / "work"
    workdir.mkdir()

    proc = _run_runner(pkg_dir, workdir, "--prompt", "p", *extra_args)

    assert proc.returncode != 0, proc.stdout + proc.stderr
    terminal = _events(proc.stdout)[-1]
    assert terminal["event"] == "error"
    assert terminal["status"] == "failed"
    assert expected_fragment in str(terminal["error"])


def test_runner_imports_a_runtime_living_in_the_workdir(tmp_path: Path) -> None:
    """``sys.path[0]`` is the runner's own directory, not the task worktree.

    Without appending the workdir, a runtime checked into the task worktree is
    unimportable no matter how it is configured. Appended rather than
    prepended, so a worktree file cannot shadow an installed distribution.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "worktree_runtime.py").write_text(
        "def chat(*, prompt, model, workdir):\n    return 'from the worktree'\n",
        encoding="utf-8",
    )

    # Empty PYTHONPATH: the workdir is the only place this module exists.
    proc = _run_runner(tmp_path / "nothing-here", workdir, "--prompt", "p", "--runtime-module", "worktree_runtime")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _events(proc.stdout)[-1]["output"] == "from the worktree"


def test_runtime_stdout_never_corrupts_the_event_stream(tmp_path: Path) -> None:
    """The runtime is arbitrary code; its prints must not land in the JSONL."""
    pkg_dir = _write_runtime(
        tmp_path,
        """
        import sys

        def chat(*, prompt, model, workdir):
            print("not a json event")
            sys.stdout.write("neither is this\\n")
            return "done"
        """,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    proc = _run_runner(pkg_dir, workdir, "--prompt", "p", "--runtime-module", "fake_runtime")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Every stdout line parses as JSON - the assertion _events() would trip on.
    events = _events(proc.stdout)
    assert [e["event"] for e in events] == ["start", "result"]
    assert "not a json event" not in proc.stdout
    assert "not a json event" in proc.stderr
    assert "neither is this" in proc.stderr


def test_runner_reports_a_runtime_that_exits_zero_as_failed(tmp_path: Path) -> None:
    """``SystemExit(0)`` from the runtime must not unwind into a clean exit.

    ``except Exception`` does not catch ``SystemExit``, so a runtime calling
    ``sys.exit(0)`` would end the process with status 0 and no terminal event -
    the same false success the error-path exit status exists to prevent.
    """
    pkg_dir = _write_runtime(
        tmp_path,
        """
        import sys

        def chat(*, prompt, model, workdir):
            sys.exit(0)
        """,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    proc = _run_runner(pkg_dir, workdir, "--prompt", "p", "--runtime-module", "fake_runtime")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    terminal = _events(proc.stdout)[-1]
    assert terminal["event"] == "error"
    assert terminal["status"] == "failed"
    assert "SystemExit" in str(terminal["error"])


def test_runner_requires_a_runtime_module(tmp_path: Path) -> None:
    """The no-runtime fallback that always reported success is gone."""
    workdir = tmp_path / "work"
    workdir.mkdir()

    proc = _run_runner(tmp_path, workdir, "--prompt", "p")

    assert proc.returncode != 0
    assert "--runtime-module" in proc.stderr
    assert proc.stdout.strip() == ""
