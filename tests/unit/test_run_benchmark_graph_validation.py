"""The offline YAML benchmark refuses a task graph that cannot finish (#7941).

``load_task`` accepted an unknown dependency and a dependency cycle. The scheduler then stopped with
steps left, and the run exited 0 and wrote a report: 12, 5 and 5 minutes for an unknown dependency,
12, 0 and 0 for a two-step cycle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_HARNESS = Path(__file__).parent.parent.parent / "benchmarks" / "run_benchmark.py"


def _import_harness() -> Any:
    spec = importlib.util.spec_from_file_location("run_benchmark_graph", _HARNESS)
    if spec is None or spec.loader is None:
        pytest.skip("benchmarks/run_benchmark.py not found")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_benchmark_graph"] = mod
    spec.loader.exec_module(mod)
    return mod


_harness = _import_harness()


def _task(subtasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "t-graph", "name": "graph", "description": "d", "category": "c", "subtasks": subtasks}


def _step(sid: str, minutes: float, deps: list[str] | None = None) -> dict[str, Any]:
    return {"id": sid, "role": "backend", "description": sid, "estimated_minutes": minutes, "depends_on": deps or []}


UNKNOWN = _task([_step("a", 5), _step("b", 7, ["missing"])])
CYCLE = _task([_step("a", 5, ["b"]), _step("b", 7, ["a"])])
FORWARD = _task([_step("b", 7, ["a"]), _step("a", 5)])


def _write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    tasks = tmp_path / "tasks"
    tasks.mkdir(exist_ok=True)
    path = tasks / "task_graph.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def test_an_unknown_dependency_names_the_task_and_the_step(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"t-graph.*'b'.*'missing'"):
        _harness.load_task(_write(tmp_path, UNKNOWN))


def test_a_cycle_names_the_task_and_the_steps(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"t-graph.*cycle.*a.*b"):
        _harness.load_task(_write(tmp_path, CYCLE))


def test_a_forward_reference_keeps_its_result(tmp_path: Path) -> None:
    task = _harness.load_task(_write(tmp_path, FORWARD))
    assert _harness.simulate_schedule(task, 1) == 12
    assert _harness.simulate_schedule(task, 3) == 12


@pytest.mark.parametrize("doc", [UNKNOWN, CYCLE], ids=["unknown-dependency", "cycle"])
def test_the_command_exits_nonzero_and_writes_no_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], doc: dict[str, Any]
) -> None:
    _write(tmp_path, doc)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--tasks-dir", str(tmp_path / "tasks"), "--output", str(out)])
    with pytest.raises(SystemExit) as exc:
        _harness.main()
    assert exc.value.code not in (0, None)
    assert "t-graph" in capsys.readouterr().err
    assert not out.exists() or not any(out.iterdir())
